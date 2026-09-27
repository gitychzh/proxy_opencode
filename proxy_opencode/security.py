"""Gateway-side bearer auth + per-key rate limiting."""

from __future__ import annotations

import logging

from fastapi import Request

from .errors import GatewayHttpError, openai_error

logger = logging.getLogger("proxy_opencode.security")


def build_auth_dependency(settings):
    """Return a FastAPI dependency that enforces the gateway key + rate limit."""

    async def require_gateway_key(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if settings.dev_open:
            return token or "dev-open"
        if not token or token not in settings.gateway_api_keys:
            # Audit log: presence of a credential, never its value.
            logger.warning(
                "gateway auth rejected",
                extra={
                    "client": request.client.host if request.client else "-",
                    "reason": "invalid_key",
                    "provided": bool(token),
                },
            )
            raise GatewayHttpError(
                openai_error(
                    401,
                    "Invalid or missing gateway API key.",
                    err_type="authentication_error",
                    code="invalid_api_key",
                )
            )
        if not request.app.state.ratelimiter.allow(token):
            logger.warning(
                "gateway rate limited",
                extra={
                    "client": request.client.host if request.client else "-",
                    "reason": "rate_limited",
                    "provided": True,
                },
            )
            raise GatewayHttpError(
                openai_error(
                    429,
                    "Rate limit exceeded for this gateway API key.",
                    err_type="rate_limit_error",
                    code="rate_limit_exceeded",
                )
            )
        return token

    return require_gateway_key
