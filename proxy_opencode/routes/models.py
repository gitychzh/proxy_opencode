"""GET /v1/models and GET /healthz."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

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

    async def _models_cus_key(request: Request) -> str:
        """models_cus auth: Bearer header OR ``?key=`` (browser friendly).

        A browser paste cannot set an Authorization header, so the hidden
        catalog accepts the gateway/admin key via the ``key`` query param
        as well. This dependency is models_cus-only on purpose — the main
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
        "/v1/models_cus",
        response_model=None,
        include_in_schema=False,
    )
    async def list_models_cus(request: Request, _k: str = Depends(_models_cus_key)) -> Any:
        """Hidden list of the relayed upstream model ids.

        Deliberately NOT under /v1/models or any standard spelling: the
        standard path must keep serving only the masked public alias so
        model-scanning agents find nothing. The ``_cus`` suffix IS the
        contract — never alias it to a standard-looking name.

        Only the configured relay models are listed (OPENCODE_ZEN_MODELS,
        default ``big-pickle``) — nothing else is proxied, and knowing the
        raw id does not make it callable (chat accepts the public alias
        only). Default body is bare ids, one per line, ``text/plain`` with
        no trailing newline (browser page, owner 2026-10-16);
        ``?format=json`` returns the OpenAI-shaped envelope instead.
        """

        catalog = getattr(request.app.state, "free_catalog", None)
        if catalog is None:
            return openai_error(
                404, "models_cus catalog is available only in zen-direct mode."
            )
        if request.query_params.get("format", "").strip().lower() == "json":
            return catalog.body()
        return Response(
            content=catalog.plain_body(),
            media_type="text/plain; charset=utf-8",
        )

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        adapter = request.app.state.adapter
        status = (
            adapter.fallback_status()
            if hasattr(adapter, "fallback_status")
            else None
        )
        return {
            "status": "ok",
            "version": __version__,
            "upstream_mode": settings.upstream_mode,
            "dev_open": settings.dev_open,
            **({"fallback": status} if status is not None else {}),
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
