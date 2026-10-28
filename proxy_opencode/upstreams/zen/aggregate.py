"""SSE aggregation: one upstream stream -> a single chat-completion JSON body.

The free tier only serves ``stream: true`` requests, so a non-streaming client
is served by aggregating the SSE the adapter always receives. Two behaviours
are load-bearing:

* a stream that ends with content but no terminal chunk is a **cut**: it is
  reported as ``finish_reason: "length"`` (never ``"stop"``), so a client sees
  an output-cap truncation instead of a protocol violation;
* ``created`` arriving as a malformed value must not abort the aggregation —
  it used to escape as a bare ``ValueError`` and reach the client as a fake 400.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any

from ..openai_http import ResponseLike


class StreamStalled(ConnectionError):
    """No body line arrived within the stall window (``STREAM_STALL_S``).

    Subclasses ``ConnectionError`` on purpose: the bucket-failover loop in
    ``ZenDirectAdapter.chat`` already treats that as "this bucket is dead,
    replay the same body on the next one" — which is the correct recovery
    here, because a non-streaming client has received **nothing** yet. The
    streaming path cannot do that (bytes already left the wire), so
    ``StreamRelay`` synthesises a terminator instead; the two paths must not
    be conflated.
    """


async def _lines_with_stall(
    resp: ResponseLike, stall_s: float
) -> AsyncIterator[str]:
    """Iterate upstream lines, converting a silent gap into ``StreamStalled``."""

    iterator = resp.aiter_lines()
    while True:
        try:
            if stall_s > 0:
                line = await asyncio.wait_for(iterator.__anext__(), timeout=stall_s)
            else:
                line = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            # Measured 2026-10-27 18:01Z / 18:05Z: the bun hop delivered
            # response HEADERS (so ``forward.ts``'s TTFB timer never fired)
            # and then went silent for the whole 90s primary budget; the
            # client got an answer 96s/98s later from the backup model with
            # no attempt to use a healthy bucket. Nothing bounded this path
            # but ``ZEN_FALLBACK_PRIMARY_TIMEOUT_S``.
            raise StreamStalled(
                "zen request failed: aggregate stalled, no body line for "
                f"{stall_s:g}s"
            ) from exc
        yield line


def merge_tool_call_delta(
    acc: dict[int, dict[str, Any]], delta: dict[str, Any]
) -> None:
    """Merge one streamed tool_call delta into the per-index accumulator."""

    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        idx = int(tc.get("index") or 0)
        slot = acc.setdefault(
            idx,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


async def aggregate_sse(
    resp: ResponseLike, model: str, *, stall_s: float = 0.0
) -> dict[str, Any]:
    """Aggregate one upstream SSE stream into a non-stream completion JSON.

    ``stall_s`` bounds the wait for the *next body line*; ``0`` disables the
    guard. Callers on the live path pass ``STREAM_STALL_S`` so the
    non-streaming route has the same watchdog discipline as the streaming
    one instead of hanging until the outer primary deadline.
    """

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    completion_id = ""
    created = int(time.time())

    async for line in _lines_with_stall(resp, stall_s):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue  # tolerate the non-standard {"choices":[],"cost":"0"} tail
        if not isinstance(chunk, dict):
            continue
        if chunk.get("id"):
            completion_id = str(chunk["id"])
        if chunk.get("created"):
            try:
                created = int(chunk["created"])
            except (TypeError, ValueError):
                pass
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(str(delta["content"]))
            if delta.get("reasoning_content"):
                reasoning_parts.append(str(delta["reasoning_content"]))
            merge_tool_call_delta(tool_calls, delta)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    ordered = [tool_calls[i] for i in sorted(tool_calls)]
    if ordered:
        message["tool_calls"] = ordered
    if tool_calls and finish_reason is None:
        finish_reason = "tool_calls"
    cut = finish_reason is None and bool(content_parts or reasoning_parts)
    final_reason = finish_reason or ("length" if cut else "stop")
    return {
        "id": completion_id or f"chatcmpl-zen-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": final_reason}
        ],
        "usage": usage,
        "metadata": {
            "adapter": "zen-direct",
            "aggregated_stream": True,
            **({"upstream_stream_cut": True} if cut else {}),
        },
    }


__all__ = ["StreamStalled", "aggregate_sse", "merge_tool_call_delta"]
