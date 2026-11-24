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
from .egress.free_models import FreeModelsSnapshot
from .egress.pool import EgressPool
from .egress.rotate import EgressRotator
from .errors import GatewayHttpError
from .logsetup import setup_logging
from .registry import ModelRegistry
from .routes import admin as admin_routes
from .routes import anthropic_api as anthropic_routes
from .routes import chat as chat_routes
from .routes import models as models_routes
from .routes import responses_api as responses_routes
from .upstreams import build_adapter

logger = logging.getLogger("proxy_opencode")


def _warn_about_unrecoverable_pool(
    settings: Settings, rotator: EgressRotator | None
) -> None:
    """Surface the settings combinations that leave the pool unable to heal.

    Each of these was part of the 2026-11-24 outage, and each fails
    silently: the gateway keeps serving, the ledger looks plausible, and
    the pool simply never gets better. Warnings (not errors) because a
    deployment without device-side rotation is legitimate — it just has to
    know that a quota verdict is then permanent until the window resets.
    """

    if rotator is None:
        logger.warning(
            "no egress rotation is configured (ZEN_EGRESS_ROTATION / "
            "ZEN_EGRESS_TOKEN_ROTATION empty): a per-IP quota verdict on "
            "every bucket leaves the gateway with no way back until the "
            "08:00 window reset"
        )
        return
    if not settings.zen_rotate_on_hard_429:
        logger.warning(
            "ZEN_ROTATE_ON_HARD_429 is off: a hard 429 will NOT re-dial the "
            "device by itself. Pool-wide exhaustion still escalates "
            "(rotation on the last usable bucket), but a single burned "
            "bucket only returns after ZEN_EGRESS_ROTATE_MIN_INTERVAL_S "
            "passes and a threshold fires"
        )
    for name, budget in settings.zen_egress_token_rotation.items():
        threshold = settings.zen_egress_rotation.get(name, 0)
        if budget > 5_000_000 or (threshold and threshold > 200):
            logger.warning(
                "rotation threshold for %s looks unreachable "
                "(requests=%s tokens=%s): the upstream burned a whole day's "
                "allowance in ~1.8M estimated tokens on 2026-10-29, so a "
                "multi-million budget never fires before the hard 429 does",
                name,
                threshold,
                budget,
            )
    if settings.zen_egress_last_resort:
        logger.warning(
            "ZEN_EGRESS_LAST_RESORT=%s: these buckets only receive traffic "
            "when nothing else can serve. Make sure the device is on its "
            "expected network — a wrong-network leg reports ready=true and "
            "resets every tunnel",
            ",".join(sorted(settings.zen_egress_last_resort)),
        )


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
    free_snapshot = (
        FreeModelsSnapshot(settings.zen_free_models_file)
        if settings.zen_free_models_file
        else None
    )
    app.state.free_models_snapshot = free_snapshot
    app.state.registry = ModelRegistry(settings, free_snapshot)
    # P3: the hidden /v1/models_cus endpoint's data source. With a daily
    # free-model snapshot configured, the relay allowlist is lifted so the
    # hidden endpoint tracks the same live free catalogue as /v1/models.
    free_catalog: ZenFreeCatalog | None = None
    if settings.upstream_mode == "zen-direct":
        free_catalog = ZenFreeCatalog(
            settings.zen_base_url,
            api_key=settings.zen_api_key,
            relay_ids=None if free_snapshot is not None else settings.zen_models,
        )
    app.state.free_catalog = free_catalog

    if egress_pool is not None and settings.upstream_mode != "zen-direct":
        logger.warning(
            "ZEN_EGRESS_PROXIES is set but UPSTREAM_MODE=%s does not use the "
            "egress pool (only zen-direct does); the pool will idle",
            settings.upstream_mode,
        )
    if egress_pool is not None:
        _warn_about_unrecoverable_pool(settings, egress_rotator)

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

    Configures logging first: the container (deploy/ecs/Dockerfile) starts
    through this factory instead of `python -m proxy_opencode`, so without
    this call no one ever ran `setup_logging()` in production — the root
    logger had no handler, every INFO record (request stats, stream
    lifecycle) was silently dropped and WARNINGs fell through to the
    unformatted lastResort handler (found 2026-10-19 while attributing a
    stream drop to an egress bucket: docker logs carried no bucket fields).
    uvicorn's LOGGING_CONFIG has no `root` key, so this survives its
    dictConfig in either order.
    """
    setup_logging()
    return create_app(load_settings())
