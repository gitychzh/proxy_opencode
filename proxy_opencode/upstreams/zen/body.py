"""Request body construction for the free tier.

Three gates ride on the body (AGENTS.md 硬约束 1): the marker prompt (body
gate), the injected builtin tools (shape gate) and ``stream: true`` (forced by
the caller). This module owns the composition and nothing else — no I/O, no
settings object — so it is directly unit-testable.
"""

from __future__ import annotations

from typing import Any

from ...errors import InvalidRequest
from .prompt import BRIDGE_NOTE, BRIDGE_NOTE_STANDALONE, render_prompt
from .tools import merge_tools

# Only OpenAI-core per-message fields are forwarded; the upstream rejects any
# extra key.
_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def clean_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Keep only OpenAI-core per-message fields (upstream rejects extras).

    A message left with neither ``content`` nor ``tool_calls`` (e.g. an
    assistant turn whose only part was internal reasoning) is dropped: the
    upstream rejects such turns with HTTP 400 and would fail the whole
    request.
    """

    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        cleaned = {k: v for k, v in m.items() if k in _MESSAGE_KEYS and v is not None}
        if "content" not in cleaned and not cleaned.get("tool_calls"):
            continue
        out.append(cleaned)
    return out


def _inject_marker(
    messages: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    """Prepend the captured opencode prompt rendered for this request."""

    marker: dict[str, Any] = {
        "role": "system",
        "content": render_prompt(model) + BRIDGE_NOTE,
    }
    return [marker, *messages]


def _inject_bridge(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepend only the tiny tool-steering note (no opencode prompt)."""

    marker: dict[str, Any] = {
        "role": "system",
        "content": BRIDGE_NOTE_STANDALONE,
    }
    return [marker, *messages]


def build_body(
    payload: dict[str, Any],
    *,
    model: str,
    auth_free: bool,
    marker_mode: str,
    tools_mode: str,
    builtins: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose one upstream body from the whitelisted client payload.

    ``auth_free`` selects the anonymous-free-tier shaping (marker + injected
    builtin tools); a keyed deployment passes the client's tools through
    untouched. ``stream`` is not set here — the adapter forces it, because the
    free tier 403s any non-streaming request.
    """

    body = {k: v for k, v in payload.items() if k != "model"}
    body["model"] = model
    messages = clean_messages(body.get("messages") or [])
    if not messages:
        raise InvalidRequest(
            "messages must contain at least one usable turn "
            "(content or tool_calls)"
        )
    client_tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    if auth_free:
        if marker_mode == "full":
            messages = _inject_marker(messages, model)
        elif marker_mode == "bridge":
            messages = _inject_bridge(messages)
        # "none": leave the client's messages untouched
        body["tools"] = merge_tools(client_tools, mode=tools_mode, builtins=builtins)
        if "tool_choice" not in body and body["tools"]:
            body["tool_choice"] = "auto"
    else:
        body["tools"] = client_tools
    if not body.get("tools"):
        body.pop("tools", None)
        body.pop("tool_choice", None)
    body["messages"] = messages
    # Mirror the genuine client's stream_options (the adapter forces stream).
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


__all__ = ["build_body", "clean_messages"]
