"""Shared request-normalisation helpers for adapters."""

from __future__ import annotations

from typing import Any

# Fields forwarded verbatim when present in chat completion requests.
PASSTHROUGH_FIELDS = (
    "model",
    "messages",
    "stream",
    "tools",
    "tool_choice",
    "response_format",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "user",
    "logprobs",
    "top_logprobs",
    "parallel_tool_calls",
)
# Reasoning-related fields, only forwarded when REASONING_PASSTHROUGH=true.
REASONING_FIELDS = (
    "reasoning_effort",
    "thinking",
    "include_reasoning",
    "reasoning",
)


def filter_payload(body: dict[str, Any], reasoning_passthrough: bool) -> dict[str, Any]:
    """Whitelist-filter an incoming chat completion body."""
    allowed = list(PASSTHROUGH_FIELDS)
    if reasoning_passthrough:
        allowed += list(REASONING_FIELDS)
    return {k: v for k, v in body.items() if k in allowed and v is not None}
