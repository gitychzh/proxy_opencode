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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        aclose = getattr(app.state.adapter, "aclose", None)
        if aclose is not None:
            await aclose()

    app = FastAPI(title="proxy_opencode", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.adapter = build_adapter(settings)
    app.state.keystore = KeyStore(settings.key_store_path, settings.key_default_ttl_hours)
    app.state.registry = ModelRegistry(settings)

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
    app.include_router(admin_routes.make_router(settings, app.state.keystore))
    return app


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --factory proxy_opencode.app:create_app_from_env`.

    Deliberately no module-level `app` instance: building one at import time
    would create an httpx client, read the key store and validate the
    environment as a side effect of merely importing this module.
    """
    return create_app(load_settings())
