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
