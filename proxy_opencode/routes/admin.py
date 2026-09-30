"""Admin API: mint / list / revoke gateway API keys.

All endpoints require an ADMIN_API_KEY bearer. Minted keys default to a 24h
validity (KEY_DEFAULT_TTL_HOURS); ttl_hours=0 mints a permanent key.

Errors use the same OpenAI error envelope and status codes as the rest of the
gateway (400 for a bad request, 404 for an unknown key id) — a client should
never have to parse a 200 body to discover that its request failed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth import build_admin_dependency
from ..auth.keystore import KeyStore
from ..config import Settings
from ..errors import openai_error


def make_router(settings: Settings, keystore: KeyStore) -> APIRouter:
    admin = build_admin_dependency(settings)
    router = APIRouter()

    @router.post("/admin/keys")
    async def create_key(
        request: Request, _: str = Depends(admin)
    ) -> Any:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        name = str(body.get("name") or "")[:64]
        try:
            ttl = float(body.get("ttl_hours", settings.key_default_ttl_hours))
        except (TypeError, ValueError):
            return openai_error(
                400,
                "ttl_hours must be a number (0 = permanent).",
                err_type="invalid_request_error",
            )
        if ttl < 0:
            return openai_error(
                400, "ttl_hours must be >= 0 (0 = permanent).", err_type="invalid_request_error"
            )
        record = keystore.create(name=name, ttl_hours=ttl)
        return {
            "id": record.id,
            "key": record.key,
            "name": record.name,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "ttl_hours": ttl if ttl > 0 else None,
        }

    @router.get("/admin/keys")
    async def list_keys(_: str = Depends(admin)) -> dict[str, Any]:
        return {"keys": [r.public_view() for r in keystore.list()]}

    @router.delete("/admin/keys/{key_id}")
    async def revoke_key(key_id: str, _: str = Depends(admin)) -> JSONResponse:
        ok = keystore.revoke(key_id)
        if not ok:
            return openai_error(
                404,
                f"Key id {key_id!r} not found (or already revoked).",
                err_type="invalid_request_error",
            )
        return JSONResponse(status_code=200, content={"revoked": key_id})

    return router
