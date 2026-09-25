"""GET /v1/models and GET /healthz."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..errors import openai_error
from ..security import build_auth_dependency


def make_router(settings: Settings) -> APIRouter:
    auth = build_auth_dependency(settings)
    router = APIRouter()

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "upstream_mode": settings.upstream_mode,
            "dev_open": settings.dev_open,
        }

    @router.get("/v1/models")
    async def list_models(request: Request, _: str = Depends(auth)) -> Any:
        adapter = request.app.state.adapter
        try:
            result = await adapter.list_models()
        except (ConnectionError, httpx.HTTPError):
            return openai_error(502, "Cannot reach upstream models endpoint.")
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
