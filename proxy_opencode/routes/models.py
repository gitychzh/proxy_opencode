"""GET /v1/models and GET /healthz."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..auth import build_auth_dependency
from ..auth.core import _bearer_token, _reject, classify_key
from ..auth.keystore import KeyStore
from ..config import Settings
from ..errors import openai_error
from ..registry import ModelRegistry


def make_router(
    settings: Settings, keystore: KeyStore, registry: ModelRegistry
) -> APIRouter:
    auth = build_auth_dependency(settings, keystore)
    router = APIRouter()

    async def _models1_key(request: Request) -> str:
        """models1 auth: Bearer header OR ``?key=`` (browser friendly).

        A browser paste cannot set an Authorization header, so the hidden
        catalog accepts the gateway/admin key via the ``key`` query param
        as well. This dependency is models1-only on purpose — the main
        protocol routes must stay header-only (keys never land in URLs
        there, and proxies log full URLs).
        """

        if settings.dev_open:
            return (
                _bearer_token(request)
                or request.query_params.get("key", "").strip()
                or "dev-open"
            )
        token = (
            _bearer_token(request)
            or request.query_params.get("key", "").strip()
        )
        _, valid = classify_key(token, settings, keystore)
        if not valid:
            raise _reject("invalid_key", request)
        return token

    @router.get(
        "/v1/models1",
        response_model=None,
        include_in_schema=False,
    )
    async def list_models1(request: Request, _k: str = Depends(_models1_key)) -> Any:
        """Hidden full free-model catalog (P3).

        Deliberately NOT under /v1/models, /v1/model or any standard
        spelling: the standard path must keep serving only the masked
        public alias so model-scanning agents find nothing. The trailing
        "1" IS the contract — never alias it to something normal.
        """

        catalog = getattr(request.app.state, "free_catalog", None)
        if catalog is None:
            return openai_error(
                404, "models1 catalog is available only in zen-direct mode."
            )
        return catalog.body()

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "upstream_mode": settings.upstream_mode,
            "dev_open": settings.dev_open,
        }

    @router.get("/v1/models")
    async def list_models(request: Request, _: str = Depends(auth)) -> Any:
        # Masking on: users only ever see the public catalogue.
        if registry.enabled:
            return registry.catalog()
        adapter = request.app.state.adapter
        try:
            result = await adapter.list_models()
        except (ConnectionError, httpx.HTTPError):
            return openai_error(502, "Cannot reach upstream models endpoint.")
        except Exception:
            # An unexpected adapter failure must degrade to a 502 envelope,
            # never a bare 500 with a traceback-shaped body.
            return openai_error(502, "Upstream models endpoint failed.")
        if isinstance(result, httpx.Response):
            try:
                content = result.json()
            except Exception:
                content = {
                    "error": {
                        "message": f"Upstream returned non-JSON ({result.status_code}).",
                        "type": "api_error",
                        "param": None,
                        "code": None,
                    }
                }
            return JSONResponse(status_code=result.status_code, content=content)
        return result

    return router
