"""Anthropic Messages API (/v1/messages) <-> OpenAI chat completions.

Serves Claude Code and any Anthropic-SDK client. Request side converts
`system` + content-block messages (text / tool_use / tool_result) and
`input_schema` tools; response side rebuilds the Anthropic message envelope
and, for streams, emits the canonical event sequence the SDKs require:

    message_start
    (content_block_start -> content_block_delta* -> content_block_stop)*
    message_delta -> message_stop
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from .sse_iter import parse_sse_chunks


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "tool_use": "tool_use",
    "content_filter": "refusal",
}
MAX_TOKENS_STOP = {"length", "max_tokens"}


# --------------------------------------------------------------- request


def _flatten_system(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            str(b.get("text") or "") for b in system if isinstance(b, dict)
        )
    return ""


def _blocks_to_messages(role: str, blocks: list[Any]) -> list[dict[str, Any]]:
    """Convert one Anthropic message's content blocks into chat messages.

    tool_use blocks attach to the assistant message as tool_calls;
    tool_result blocks become separate role="tool" messages (order kept).
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            # Internal reasoning is not replayable as chat content -> skip.
            continue
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
        elif btype == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    str(b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            elif not isinstance(content, str):
                content = json.dumps(content or "", ensure_ascii=False)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": content,
                }
            )
    out: list[dict[str, Any]] = []
    if role == "assistant":
        # An assistant turn that carries neither text nor tool_use (e.g. a
        # message made of only `thinking` blocks, or an empty content list)
        # would become {"role":"assistant","content":null}, which the chat
        # upstream rejects outright. Drop it instead of poisoning the request.
        if text_parts or tool_calls:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts) or None,
            }
            if tool_calls:
                message["tool_calls"] = tool_calls
            out.append(message)
    elif text_parts:
        out.append({"role": role, "content": "\n".join(text_parts)})
    out.extend(tool_messages)
    return out


def to_chat_payload(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system_text = _flatten_system(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            messages.extend(_blocks_to_messages(role, content))

    chat_tools: list[dict[str, Any]] = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name"):
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name")),
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("input_schema")
                        or {"type": "object", "properties": {}},
                    },
                }
            )

    if not messages:
        # Every turn was empty/unconvertible; the chat upstream rejects an
        # empty messages array outright, so surface it as a client error.
        raise ValueError("messages must contain at least one usable turn")

    payload: dict[str, Any] = {
        "model": str(body.get("model") or ""),
        "messages": messages,
        "stream": bool(body.get("stream")),
        # Anthropic requires max_tokens; chat upstreams accept it directly.
        "max_tokens": int(body.get("max_tokens") or 4096),
    }
    if chat_tools:
        payload["tools"] = chat_tools
        payload["tool_choice"] = "auto"
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            payload[dst] = body[src]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]
    return payload


# -------------------------------------------------------------- response


def _chat_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Map a chat-completions usage block onto Anthropic token counters."""
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    out = {"input_tokens": prompt, "output_tokens": completion}
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    if cached:
        out["cache_read_input_tokens"] = cached
    return out


def chat_to_anthropic_body(
    chat_resp: dict[str, Any], model: str
) -> dict[str, Any]:
    message = (chat_resp.get("choices") or [{}])[0].get("message") or {}
    finish = (chat_resp.get("choices") or [{}])[0].get("finish_reason") or "stop"
    content: list[dict[str, Any]] = []
    if message.get("reasoning_content"):
        content.append(
            {"type": "thinking", "thinking": str(message["reasoning_content"])}
        )
    if message.get("content"):
        content.append({"type": "text", "text": str(message["content"])})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            tool_input = {}
        content.append(
            {
                "type": "tool_use",
                "id": str(tc.get("id") or _new_id("toolu")),
                "name": str(fn.get("name") or ""),
                "input": tool_input,
            }
        )
    if not content:
        content = [{"type": "text", "text": ""}]
    usage = _chat_usage(chat_resp.get("usage"))
    stop_reason = "max_tokens" if finish in MAX_TOKENS_STOP else STOP_REASON_MAP.get(
        finish, "end_turn"
    )
    return {
        "id": _new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


# -------------------------------------------------------------- streaming


class _AnthropicStreamState:
    def __init__(self, model: str) -> None:
        self.model = model
        self.message_id = _new_id("msg")
        self.block_index = -1
        self.open_type: str | None = None  # "text" | "thinking" | "tool"
        self.text_buf = ""
        self.think_buf = ""
        # chat tool-call index -> block bookkeeping
        self.tool_blocks: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None

    def next_block(self) -> int:
        self.block_index += 1
        return self.block_index


def _event(name: str, data: dict[str, Any]) -> bytes:
    # The Anthropic wire format repeats the event name inside the JSON payload
    # (`{"type": "<name>", ...}`); SDKs read `data["type"]` to dispatch, so it
    # must be present even though the SSE `event:` line already carries it.
    payload = {"type": name, **data}
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


async def stream_anthropic_events(
    chunks: AsyncIterator[bytes], model: str
) -> AsyncIterator[bytes]:
    """Convert OpenAI chat SSE chunks into Anthropic Messages SSE events."""
    st = _AnthropicStreamState(model)

    def close_open_block() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            if st.open_type is None:
                return
            # The Anthropic spec carries only `type` + `index` on
            # content_block_stop; the block payload is not repeated.
            yield _event("content_block_stop", {"index": st.block_index})
            st.open_type = None

        return gen()

    yield _event(
        "message_start",
        {
            "message": {
                "id": st.message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        },
    )

    async for chunk in parse_sse_chunks(chunks):
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            st.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                st.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if reasoning:
                if st.open_type != "thinking":
                    async for ev in close_open_block():
                        yield ev
                    st.next_block()
                    st.open_type = "thinking"
                    yield _event(
                        "content_block_start",
                        {
                            "index": st.block_index,
                            "content_block": {"type": "thinking", "thinking": ""},
                        },
                    )
                st.think_buf += str(reasoning)
                yield _event(
                    "content_block_delta",
                    {
                        "index": st.block_index,
                        "delta": {"type": "thinking_delta", "thinking": str(reasoning)},
                    },
                )

            text = delta.get("content")
            if text:
                if st.open_type != "text":
                    async for ev in close_open_block():
                        yield ev
                    st.next_block()
                    st.open_type = "text"
                    yield _event(
                        "content_block_start",
                        {
                            "index": st.block_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                st.text_buf += str(text)
                yield _event(
                    "content_block_delta",
                    {
                        "index": st.block_index,
                        "delta": {"type": "text_delta", "text": str(text)},
                    },
                )

            for tc_delta in delta.get("tool_calls") or []:
                if not isinstance(tc_delta, dict):
                    continue
                idx = int(tc_delta.get("index") or 0)
                slot = st.tool_blocks.get(idx)
                if slot is None:
                    fn = tc_delta.get("function") or {}
                    slot = {
                        "id": str(tc_delta.get("id") or _new_id("toolu")),
                        "name": str(fn.get("name") or ""),
                        "block_index": 0,
                    }
                    async for ev in close_open_block():
                        yield ev
                    slot["block_index"] = st.next_block()
                    st.open_type = "tool"
                    st.tool_blocks[idx] = slot
                    yield _event(
                        "content_block_start",
                        {
                            "index": st.block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": slot["id"],
                                "name": slot["name"],
                                "input": {},
                            },
                        },
                    )
                fn = tc_delta.get("function") or {}
                if fn.get("name") and not slot["name"]:
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    yield _event(
                        "content_block_delta",
                        {
                            "index": slot["block_index"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": str(fn["arguments"]),
                            },
                        },
                    )

    async for ev in close_open_block():
        yield ev

    usage = _chat_usage(st.usage)
    finish = st.finish_reason or "stop"
    stop_reason = (
        "max_tokens" if finish in MAX_TOKENS_STOP else STOP_REASON_MAP.get(finish, "end_turn")
    )
    if st.tool_blocks:
        stop_reason = "tool_use"
    yield _event(
        "message_delta",
        {
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"]},
        },
    )
    yield _event("message_stop", {})
