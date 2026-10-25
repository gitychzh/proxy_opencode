"""Upstream adapters.

An UpstreamAdapter is the seam between the OpenAI HTTP surface
(routes/chat.py, routes/models.py) and whatever actually answers chat
requests. New upstream mode = new module implementing this protocol plus one
line in build_adapter.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..config import Settings
from ..egress.pool import EgressPool
from . import failover, openai_http, opencode_serve, zen_direct
from .openai_http import StreamRelay
from .opencode_serve import ServeCompletion

# What ``chat()`` may return:
#   * StreamRelay      -> a live upstream SSE stream (relayed / re-encoded)
#   * ServeCompletion  -> a finished serve-mode turn (normalised by the route)
#   * dict             -> a ready chat-completion JSON body, or an error relay
#                         shaped as ``{"__status__": int, "content": {...}}``
ChatResult = StreamRelay | ServeCompletion | dict[str, Any]


class UpstreamAdapter(Protocol):
    """What the routes layer needs from an upstream."""

    name: str

    async def list_models(self) -> Any:
        """OpenAI `GET /v1/models` response body (dict or httpx.Response)."""
        ...

    async def chat(self, payload: dict[str, Any]) -> ChatResult:
        """Handle one chat completion request.

        `payload` is the already-whitelisted OpenAI chat-completion body. The
        adapter may return a JSON body, a `StreamRelay` for SSE passthrough,
        a `ServeCompletion`, or an error relay; the route layer dispatches on
        the concrete type.
        """
        ...


def build_adapter(
    settings: Settings, pool: EgressPool | None = None
) -> UpstreamAdapter:
    primary: UpstreamAdapter
    if settings.is_opencode_serve:
        primary = opencode_serve.ServeAdapter(settings)
    elif settings.is_zen_direct:
        primary = zen_direct.ZenDirectAdapter(settings, pool=pool)
    else:
        primary = openai_http.OpenAIHttpAdapter(settings)
    if settings.zen_fallback_base_url:
        # Backup-chain failover (2026-10-25): wrap the primary so egress-level
        # faults (quota 429 / 5xx / transport) hand chat over to the backup
        # gateway (proxy_ms) with a prominent log + JSONL event + /healthz
        # status, and traffic returns automatically after the hold window.
        return failover.FailoverAdapter(primary, settings)
    return primary


__all__ = [
    "ChatResult",
    "ServeCompletion",
    "StreamRelay",
    "UpstreamAdapter",
    "build_adapter",
]
