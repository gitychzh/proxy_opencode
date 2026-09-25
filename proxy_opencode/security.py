"""Gateway-side bearer auth + per-key rate limiting."""

from __future__ import annotations

from fastapi import Request

from .errors import GatewayHttpError, openai_error


def build_auth_dependency(settings):
    """Return a FastAPI dependency that enforces the gateway key + rate limit."""

    async def require_gateway_key(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if settings.dev_open:
            return token or "dev-open"
        if not token or token not in settings.gateway_api_keys:
            raise GatewayHttpError(
                openai_error(
                    401,
                    "Invalid or missing gateway API key.",
                    err_type="authentication_error",
                    code="invalid_api_key",
                )
            )
        if not request.app.state.ratelimiter.allow(token):
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
