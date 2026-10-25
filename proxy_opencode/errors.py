"""OpenAI-style error responses."""

from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    status: int,
    message: str,
    err_type: str = "api_error",
    code: str | None = None,
) -> JSONResponse:
    """Build an OpenAI api-error shaped JSON response."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": err_type,
                "param": None,
                "code": code,
            }
        },
    )


class GatewayHttpError(Exception):
    """Exception carrying a ready-made OpenAI-style JSON response."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__(response.status_code)
        self.response = response


class InvalidRequest(ValueError):
    """A client-fault request shape error, safe to answer as HTTP 400.

    The pipeline used to map EVERY ``ValueError`` out of an adapter onto
    "invalid_request_error" — including accidental ones raised while parsing
    an upstream body. Production evidence (2026-10-25, 2.2s, usage=null, one
    occurrence in 24h): the client was told its request was malformed when
    the upstream answer had actually tripped an ``int()``/parse path. A fake
    400 also suppresses the failover ladder (request-level errors are never
    retried on another bucket), so the misattribution costs availability,
    not just clarity.

    Raise this — and only this — when the request itself is the problem.
    Anything else a ``ValueError``-derived exception could mean must surface
    as a 5xx.
    """
