"""OpenAI Responses API (/v1/responses) <-> OpenAI chat completions.

Serves codex CLI and any Responses-API client. Request side flattens
`instructions` + `input` (string or typed items) into chat messages and
converts the flat function-tool schema; response side rebuilds the Responses
envelope (output items, usage) and, for streams, emits the canonical event
sequence codex expects:

    response.created -> (response.output_item.added ->
    response.output_text.delta*) -> response.output_item.done ->
    response.completed
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from .sse_iter import parse_sse_chunks


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# --------------------------------------------------------------- request


def _content_text(content: Any) -> str:
    """Flatten Responses content (string or typed parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("input_text", "output_text", "text"):
                    parts.append(str(part.get("text") or ""))
                elif part.get("type") in ("input_image", "image"):
                    parts.append("[image omitted]")
    return "\n".join(parts)


def _input_items_to_messages(items: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype == "message":
            role = item.get("role") or "user"
            text = _content_text(item.get("content"))
            if role == "assistant" and not text:
                continue
            if role == "tool":
                # chat wire only allows tool messages that answer a preceding
                # assistant tool_calls message via tool_call_id. Clients that
                # replay compressed histories can emit unpaired tool turns;
                # relaying them verbatim makes upstream reject the whole
                # request (HTTP 400 invalid_request_error). Degrade any tool
                # item without a usable id into a user message instead.
                tcid = str(item.get("tool_call_id") or item.get("call_id") or "")
                if tcid:
                    messages.append(
                        {"role": "tool", "tool_call_id": tcid, "content": text}
                    )
                else:
                    fallback = f"[tool result] {text}" if text else "[tool result]"
                    messages.append({"role": "user", "content": fallback})
                continue
            messages.append({"role": role, "content": text})
        elif itype == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or "{}"),
                            },
                        }
                    ],
                }
            )
        elif itype == "function_call_output":
            output = item.get("output")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False) if output else ""
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": output,
                }
            )
        # reasoning / other item types carry no call payload -> skip
    return messages


def to_chat_payload(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(_input_items_to_messages(inp))

    chat_tools: list[dict[str, Any]] = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("parameters")
                        or {"type": "object", "properties": {}},
                    },
                }
            )

    payload: dict[str, Any] = {
        "model": str(body.get("model") or ""),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    if chat_tools:
        payload["tools"] = chat_tools
        payload["tool_choice"] = "auto"
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            payload[dst] = body[src]
    if body.get("max_output_tokens") is not None:
        payload["max_tokens"] = body["max_output_tokens"]
    if not messages:
        # Input consisted solely of skippable items (e.g. reasoning-only
        # history replay). An empty messages array is rejected outright by
        # upstream, so substitute a benign continuation prompt.
        messages.append({"role": "user", "content": "[no convertible input; continue]"})
    return payload


# -------------------------------------------------------------- response


def _usage_block(usage: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(usage, dict) or not usage:
        return {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    cached = int(prompt_details.get("cached_tokens") or 0)
    reasoning = int(completion_details.get("reasoning_tokens") or 0)
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": int(usage.get("total_tokens") or (prompt + completion)),
    }


def _response_envelope(
    response_id: str, created_at: int, model: str, output: list[dict[str, Any]],
    status: str, usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "temperature": None,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "max_output_tokens": None,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "usage": usage or {},
        "user": None,
    }
    return envelope


def _message_item(item_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def chat_to_responses_body(
    chat_resp: dict[str, Any], model: str
) -> dict[str, Any]:
    message = (chat_resp.get("choices") or [{}])[0].get("message") or {}
    output: list[dict[str, Any]] = []
    if message.get("reasoning_content"):
        output.append({"type": "reasoning", "id": _new_id("rs"), "summary": []})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": _new_id("fc"),
                "call_id": str(tc.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "arguments": str(fn.get("arguments") or "{}"),
                "status": "completed",
            }
        )
    text = message.get("content")
    if text or not output:
        output.append(_message_item(_new_id("msg"), str(text or "")))
    return _response_envelope(
        _new_id("resp"),
        int(chat_resp.get("created") or time.time()),
        model,
        output,
        "completed",
        _usage_block(chat_resp.get("usage")),
    )


# -------------------------------------------------------------- streaming


class _StreamState:
    """Accumulates one streamed chat completion into Responses output items.

    Every emitted item (the assistant message and each function call) is
    assigned a unique, monotonically increasing `output_index` from a single
    counter, so the streamed `output_item.added/done` indices always agree
    with the item order of the final `response.completed` envelope.
    """

    def __init__(self) -> None:
        self.response_id = _new_id("resp")
        self.created_at = int(time.time())
        self.seq = 0
        self.msg_id = _new_id("msg")
        self.msg_open = False
        self.msg_index: int | None = None
        self.text_buf = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.tool_announced: set[int] = set()
        self.next_item_index = 0
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def alloc_item_index(self) -> int:
        """Reserve the next unique output index for an item."""
        index = self.next_item_index
        self.next_item_index += 1
        return index

    def final_output(self) -> list[dict[str, Any]]:
        """Final `output` array, ordered by the indices used while streaming."""
        items: list[tuple[int, dict[str, Any]]] = []
        if self.text_buf or not self.tool_calls:
            items.append(
                (
                    self.msg_index if self.msg_index is not None else 0,
                    _message_item(self.msg_id, self.text_buf),
                )
            )
        for idx in sorted(self.tool_calls):
            tc = self.tool_calls[idx]
            items.append(
                (
                    tc["item_index"],
                    {
                        "type": "function_call",
                        "id": tc["item_id"],
                        "call_id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        "status": "completed",
                    },
                )
            )
        items.sort(key=lambda pair: pair[0])
        return [item for _index, item in items]


def _sse_event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def stream_responses_events(
    chunks: AsyncIterator[bytes], model: str
) -> AsyncIterator[bytes]:
    """Convert OpenAI chat SSE chunks into Responses API SSE events."""
    st = _StreamState()

    def event(name: str, payload: dict[str, Any]) -> bytes:
        payload = {"type": name, "sequence_number": st.next_seq(), **payload}
        return _sse_event(name, payload)

    def envelope(status: str) -> dict[str, Any]:
        return _response_envelope(
            st.response_id, st.created_at, model, [], status
        )

    yield event("response.created", {"response": envelope("in_progress")})
    yield event("in_progress", {"response": envelope("in_progress")})

    async for chunk in parse_sse_chunks(chunks):
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            st.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                st.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if not st.msg_open:
                    st.msg_open = True
                    st.msg_index = st.alloc_item_index()
                    item = {
                        "type": "message",
                        "id": st.msg_id,
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    yield event(
                        "response.output_item.added",
                        {
                            "output_index": st.msg_index,
                            "item": item,
                        },
                    )
                    yield event(
                        "response.content_part.added",
                        {
                            "item_id": st.msg_id,
                            "output_index": st.msg_index,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        },
                    )
                st.text_buf += str(text)
                yield event(
                    "response.output_text.delta",
                    {
                        "item_id": st.msg_id,
                        "output_index": st.msg_index,
                        "content_index": 0,
                        "delta": str(text),
                    },
                )
            for tc_delta in delta.get("tool_calls") or []:
                if not isinstance(tc_delta, dict):
                    continue
                idx = int(tc_delta.get("index") or 0)
                slot = st.tool_calls.setdefault(
                    idx,
                    {
                        "id": "",
                        "name": "",
                        "arguments": "",
                        "item_id": _new_id("fc"),
                        "item_index": 0,
                    },
                )
                if tc_delta.get("id"):
                    slot["id"] = tc_delta["id"]
                fn = tc_delta.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
                if idx not in st.tool_announced and (slot["id"] or slot["name"]):
                    st.tool_announced.add(idx)
                    slot["item_index"] = st.alloc_item_index()
                    yield event(
                        "response.output_item.added",
                        {
                            "output_index": slot["item_index"],
                            "item": {
                                "type": "function_call",
                                "id": slot["item_id"],
                                "call_id": slot["id"],
                                "name": slot["name"],
                                "arguments": "",
                                "status": "in_progress",
                            },
                        },
                    )
                # Announce the argument fragment AFTER the item exists, so
                # clients never see a delta for an unknown output_index.
                if fn.get("arguments") and idx in st.tool_announced:
                    yield event(
                        "response.function_call_arguments.delta",
                        {
                            "item_id": slot["item_id"],
                            "output_index": slot["item_index"],
                            "delta": str(fn["arguments"]),
                        },
                    )

    # Close the text item.
    if st.msg_open:
        yield event(
            "response.output_text.done",
            {
                "item_id": st.msg_id,
                "output_index": st.msg_index,
                "content_index": 0,
                "text": st.text_buf,
            },
        )
        yield event(
            "response.content_part.done",
            {
                "item_id": st.msg_id,
                "output_index": st.msg_index,
                "content_index": 0,
                "part": {
                    "type": "output_text",
                    "text": st.text_buf,
                    "annotations": [],
                },
            },
        )
        yield event(
            "response.output_item.done",
            {"output_index": st.msg_index, "item": _message_item(st.msg_id, st.text_buf)},
        )
    # Close function-call items.
    for idx in sorted(st.tool_calls):
        slot = st.tool_calls[idx]
        yield event(
            "response.function_call_arguments.done",
            {
                "item_id": slot["item_id"],
                "output_index": slot["item_index"],
                "arguments": slot["arguments"],
            },
        )
        yield event(
            "response.output_item.done",
            {
                "output_index": slot["item_index"],
                "item": {
                    "type": "function_call",
                    "id": slot["item_id"],
                    "call_id": slot["id"],
                    "name": slot["name"],
                    "arguments": slot["arguments"],
                    "status": "completed",
                },
            },
        )

    final = envelope("completed")
    final["output"] = st.final_output()
    final["usage"] = _usage_block(st.usage)
    yield event("response.completed", {"response": final})
