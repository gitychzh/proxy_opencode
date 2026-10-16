"""Admin API: mint / list / revoke gateway API keys.

All endpoints require an ADMIN_API_KEY bearer. Minted keys default to a 24h
validity (KEY_DEFAULT_TTL_HOURS); ttl_hours=0 mints a permanent key.

Errors use the same OpenAI error envelope and status codes as the rest of the
gateway (400 for a bad request, 404 for an unknown key id) — a client should
never have to parse a 200 body to discover that its request failed.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth import build_admin_dependency
from ..auth.keystore import KeyStore
from ..config import Settings
from ..egress.pool import EgressPool
from ..egress.rotate import EgressRotator
from ..errors import openai_error


def make_router(
    settings: Settings,
    keystore: KeyStore,
    egress_pool: EgressPool | None = None,
    egress_rotator: EgressRotator | None = None,
) -> APIRouter:
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
        # NaN compares False against every bound, so `ttl < 0` alone would let
        # `{"ttl_hours": NaN}` slip through to the keystore — where
        # `effective_ttl > 0` is also False and the key mints as *permanent*,
        # silently defeating the default 24h expiry. Infinity overflows
        # `timedelta(hours=...)` into a 500. Both are rejected outright.
        if not math.isfinite(ttl) or ttl < 0:
            return openai_error(
                400,
                "ttl_hours must be a finite number >= 0 (0 = permanent).",
                err_type="invalid_request_error",
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

    @router.get("/admin/egresses")
    async def list_egresses(_: str = Depends(admin)) -> dict[str, Any]:
        """Egress-pool status: health, cordon, in-flight and observed IPs.

        With P5 rotation configured the per-bucket counters/rotation state
        rides along. Never includes agent credentials or message content;
        egress names, IPs and counters are the documented-safe surface.
        """
        if egress_pool is None:
            return {"pool": False, "egresses": [], "counters": {}}
        payload: dict[str, Any] = {"pool": True, "egresses": egress_pool.snapshot()}
        if egress_rotator is not None:
            payload["counters"] = egress_rotator.counters.snapshot()
        else:
            payload["counters"] = {}
        return payload

    @router.post("/admin/egresses/{name}/rotate")
    async def rotate_egress(name: str, _: str = Depends(admin)) -> Any:
        """Manually trigger one ONT rotation for a configured bucket.

        For the device-side work this exists for: field-testing the
        wifi/cc9 ONT flows and recovering a bucket whose IP was re-issued
        unchanged. Threshold- and hard-429-triggers go through the same
        path; at most one rotation per bucket runs at a time.
        """
        if egress_pool is None or egress_pool.find(name) is None:
            return openai_error(
                404,
                f"Egress {name!r} not found (pool disabled or unknown name).",
                err_type="invalid_request_error",
            )
        if egress_rotator is None or not egress_rotator.rotatable(name):
            return openai_error(
                400,
                f"Egress {name!r} is not configured for rotation "
                "(missing ZEN_EGRESS_ROTATION entry).",
                err_type="invalid_request_error",
            )
        task = egress_rotator.request_rotation(name, trigger="admin")
        if task is None:
            return JSONResponse(
                status_code=200,
                content={"started": False, "name": name, "already_rotating": True},
            )
        return {"started": True, "name": name}

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
