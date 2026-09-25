"""Upstream adapters.

An UpstreamAdapter is the seam between the OpenAI HTTP surface
(routes/chat.py, routes/models.py) and whatever actually answers chat
requests. New upstream mode = new module implementing this protocol plus one
line in build_adapter.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..config import Settings
from . import openai_http, opencode_serve


class UpstreamAdapter(Protocol):
    """What the routes layer needs from an upstream."""

    name: str

    async def list_models(self) -> dict[str, Any]:
        """OpenAI `GET /v1/models` response body."""
        ...

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle one chat completion request.

        `payload` is the already-whitelisted OpenAI chat-completion body.
        Returns an OpenAI response body (non-stream or synthetic-stream;
        adapters set `metadata.adapter`).

        For real SSE passthrough (openai mode) the adapter instead raises
        `PassThroughStream` handled by the route layer.
        """
        ...


def build_adapter(settings: Settings) -> UpstreamAdapter:
    if settings.is_opencode_serve:
        return opencode_serve.ServeAdapter(settings)
    return openai_http.OpenAIHttpAdapter(settings)
