"""FastAPI application factory: wiring only.

All behavior lives in dedicated modules:
  config.py            env/Settings
  security.py          gateway bearer auth + rate limiting
  errors.py            OpenAI-style error body helper
  ratelimit.py         fixed-window limiter
  upstreams/           per-mode adapters (protocol, openai_http, opencode_serve)
  routes/              thin HTTP handlers
"""

from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, load_settings
from .errors import GatewayHttpError
from .ratelimit import RateLimiter
from .routes import chat as chat_routes
from .routes import models as models_routes
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
    app.state.ratelimiter = RateLimiter(settings.requests_per_minute)
    app.state.adapter = build_adapter(settings)

    @app.exception_handler(GatewayHttpError)
    async def _handle_gateway_error(
        request: Request, exc: GatewayHttpError
    ) -> JSONResponse:
        return exc.response

    app.include_router(models_routes.make_router(settings))
    app.include_router(chat_routes.make_router(settings))
    return app


app = create_app()
