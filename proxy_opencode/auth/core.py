"""Gateway authentication: bearer-key validation with expiry support.

Key classes, in evaluation order:
  1. ADMIN_API_KEYS  — permanent; additionally grant /admin/* access.
  2. GATEWAY_API_KEYS — permanent (back-compat static bucket keys).
  3. KeyStore records — dynamic, expiry/revocation enforced.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from ..errors import GatewayHttpError
from .keystore import KeyStore

logger = logging.getLogger("proxy_opencode.auth")


def _key_in(token: str, keys) -> bool:
    """Constant-time membership test for static key collections."""
    for candidate in keys or []:
        try:
            if hmac.compare_digest(token, candidate):
                return True
        except TypeError:
            continue
    return False


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _client(request: Request) -> str:
    return request.client.host if request.client else "-"


def _reject(reason: str, request: Request, error_style: str = "openai") -> GatewayHttpError:
    logger.warning(
        "gateway auth rejected",
        extra={
            "client": _client(request),
            "reason": reason,
            "provided": bool(_bearer_token(request) or request.headers.get("x-api-key")),
        },
    )
    if error_style == "anthropic":
        body = {
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Invalid, expired, or missing API key.",
            },
        }
    else:
        body = {
            "error": {
                "message": "Invalid, expired, or missing gateway API key.",
                "type": "authentication_error",
                "param": None,
                "code": "invalid_api_key",
            }
        }
    return GatewayHttpError(JSONResponse(status_code=401, content=body))


def classify_key(
    token: str, settings, keystore: KeyStore | None
) -> tuple[str, bool]:
    """Return (kind, valid) for a bearer token.

    kind: "admin" | "static" | "dynamic" | "none"
    """
    if not token:
        return "none", False
    if _key_in(token, settings.admin_api_keys):
        return "admin", True
    if _key_in(token, settings.gateway_api_keys):
        return "static", True
    if keystore is not None:
        record = keystore.validate(token)
        if record is not None:
            return "dynamic", True
    return "none", False


def build_auth_dependency(
    settings, keystore: KeyStore, error_style: str = "openai"
):
    """Return a FastAPI dependency enforcing the gateway key.

    error_style: "openai" (default) or "anthropic" — controls the error
    envelope used for 401 so each protocol route speaks its own dialect.
    Concurrency/rate limiting is deliberately NOT enforced here: the Zen
    upstream applies its own scheduling, and self-hosted queuing only
    penalises agent fan-out (2026-09-30 decision).
    """

    async def require_gateway_key(request: Request) -> str:
        if settings.dev_open:
            return _bearer_token(request) or "dev-open"
        token = _bearer_token(request)
        # Anthropic-style clients may present the key in x-api-key instead.
        if not token:
            token = request.headers.get("x-api-key", "").strip()
        kind, valid = classify_key(token, settings, keystore)
        if not valid:
            expired = bool(
                kind == "none"
                and token
                and keystore is not None
                and keystore.lookup_expired(token) is not None
            )
            raise _reject(
                "expired_key" if expired else "invalid_key", request, error_style
            )
        return token

    return require_gateway_key


def build_admin_dependency(settings):
    """Return a FastAPI dependency granting only ADMIN_API_KEYS access."""

    async def require_admin_key(request: Request) -> str:
        token = _bearer_token(request)
        if not token or not _key_in(token, settings.admin_api_keys):
            raise _reject("admin_key_required", request)
        return token

    return require_admin_key
