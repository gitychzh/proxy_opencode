"""FastAPI application factory: wiring only.

All behavior lives in dedicated modules:
  config.py            env/Settings
  auth/                bearer auth, expiry key store, admin dependency
  registry.py          public-model alias layer (model masking)
  sse_mask.py          stream rewriting (scrub upstream model names)
  errors.py            OpenAI-style error body helper
  upstreams/           per-mode adapters (protocol, openai_http, opencode_serve)
  routes/              thin HTTP handlers (chat, models, responses, anthropic, admin)
  formats/             protocol converters (OpenAI Responses, Anthropic Messages)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .auth.keystore import KeyStore
from .config import Settings, load_settings
from .egress.catalog import ZenFreeCatalog
from .egress.eventlog import EVENTS
from .egress.pool import EgressPool
from .egress.rotate import EgressRotator
from .errors import GatewayHttpError
from .registry import ModelRegistry
from .routes import admin as admin_routes
from .routes import anthropic_api as anthropic_routes
from .routes import chat as chat_routes
from .routes import models as models_routes
from .routes import responses_api as responses_routes
from .upstreams import build_adapter

logger = logging.getLogger("proxy_opencode")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # Structured egress event sink (JSONL on the ECS host): everything the
    # egress layer emits afterwards lands in this one file. Empty = off.
    EVENTS.configure(settings.zen_egress_event_log)
    # 0.7.x: when ZEN_EGRESS_PROXIES is configured, chat requests are routed
    # through per-bucket device agents. The pool starts in the lifespan (it
    # opens httpx clients and a probe task) so importing this module stays
    # side-effect free.
    egress_pool = EgressPool.from_settings(settings)
    # P5: with rotation thresholds configured, the rotator rides on the
    # pool's release path (counters -> threshold / hard-429 -> ONT reboot).
    egress_rotator = None
    if egress_pool is not None:
        egress_rotator = EgressRotator.from_settings(settings, egress_pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if free_catalog is not None:
            await free_catalog.start()
        if egress_pool is not None:
            await egress_pool.start()
        try:
            yield
        finally:
            aclose = getattr(app.state.adapter, "aclose", None)
            if aclose is not None:
                await aclose()
            if free_catalog is not None:
                await free_catalog.aclose()
            if egress_rotator is not None:
                await egress_rotator.aclose()
            if egress_pool is not None:
                await egress_pool.aclose()

    app = FastAPI(title="proxy_opencode", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.egress_pool = egress_pool
    app.state.egress_rotator = egress_rotator
    app.state.adapter = build_adapter(settings, pool=egress_pool)
    app.state.keystore = KeyStore(settings.key_store_path, settings.key_default_ttl_hours)
    app.state.registry = ModelRegistry(settings)
    # P3: the hidden /v1/models_cus endpoint's data source. Each refresh also
    # seeds the registry's free-passthrough set, so chat accepts every free
    # id verbatim while /v1/models keeps serving only the masked alias.
    free_catalog: ZenFreeCatalog | None = None
    if settings.upstream_mode == "zen-direct":
        free_catalog = ZenFreeCatalog(
            settings.zen_base_url,
            api_key=settings.zen_api_key,
            registry=app.state.registry,
            fallback_ids=settings.zen_models,
        )
    app.state.free_catalog = free_catalog

    if egress_pool is not None and settings.upstream_mode != "zen-direct":
        logger.warning(
            "ZEN_EGRESS_PROXIES is set but UPSTREAM_MODE=%s does not use the "
            "egress pool (only zen-direct does); the pool will idle",
            settings.upstream_mode,
        )

    logger.info(
        "gateway starting",
        extra={
            "version": __version__,
            "mode": settings.upstream_mode,
            "bind": f"{settings.host}:{settings.port}",
            "keys": len(settings.gateway_api_keys),
            "admin_keys": len(settings.admin_api_keys),
            "public_models": [m["id"] for m in settings.public_models],
            "adapter": getattr(app.state.adapter, "name", "?"),
            "egresses": egress_pool.names() if egress_pool is not None else [],
        },
    )

    @app.exception_handler(GatewayHttpError)
    async def _handle_gateway_error(
        request: Request, exc: GatewayHttpError
    ) -> JSONResponse:
        return exc.response

    app.include_router(models_routes.make_router(settings, app.state.keystore, app.state.registry))
    app.include_router(chat_routes.make_router(settings, app.state.keystore, app.state.registry))
    app.include_router(
        responses_routes.make_router(settings, app.state.keystore, app.state.registry)
    )
    app.include_router(
        anthropic_routes.make_router(settings, app.state.keystore, app.state.registry)
    )
    app.include_router(
        admin_routes.make_router(
            settings, app.state.keystore, egress_pool, egress_rotator
        )
    )
    return app


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --factory proxy_opencode.app:create_app_from_env`.

    Deliberately no module-level `app` instance: building one at import time
    would create an httpx client, read the key store and validate the
    environment as a side effect of merely importing this module.
    """
    return create_app(load_settings())
