"""Tests for the OpenAI Responses API protocol conversion (/v1/responses)."""

from __future__ import annotations

import json

import httpx
import pytest

from proxy_opencode.formats.responses_proto import (
    chat_to_responses_body,
    stream_responses_events,
    to_chat_payload,
)
from proxy_opencode.upstreams.openai_http import StreamRelay

# --------------------------------------------------------------- request


def test_instructions_and_string_input():
    payload = to_chat_payload(
        {
            "instructions": "be brief",
            "input": "hello",
            "model": "ds41f_cus",
        }
    )
    assert payload["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    assert payload["stream"] is False


def test_typed_items_conversion():
    payload = to_chat_payload(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_time",
                    "arguments": "{\"tz\":\"utc\"}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "12:00"},
                {"type": "reasoning", "summary": [{"text": "ignored"}]},
            ]
        }
    )
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert payload["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert payload["messages"][2]["tool_call_id"] == "call_1"
    assert payload["messages"][2]["content"] == "12:00"


def test_flat_function_tools_to_chat_tools():
    payload = to_chat_payload(
        {
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "get_time",
                    "description": "returns time",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "max_output_tokens": 512,
            "temperature": 0.3,
        }
    )
    assert payload["tools"][0]["function"]["name"] == "get_time"
    assert payload["tool_choice"] == "auto"
    assert payload["max_tokens"] == 512
    assert payload["temperature"] == 0.3


# -------------------------------------------------------------- response


def test_chat_to_responses_body_text():
    chat = {
        "id": "chatcmpl-x",
        "created": 123,
        "choices": [
            {"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    out = chat_to_responses_body(chat, "ds41f_cus")
    assert out["object"] == "response"
    assert out["model"] == "ds41f_cus"
    assert out["status"] == "completed"
    assert out["output"][-1]["type"] == "message"
    assert out["output"][-1]["content"][0]["text"] == "pong"
    assert out["usage"]["input_tokens"] == 3
    assert out["usage"]["output_tokens"] == 1


def test_chat_to_responses_body_tool_calls():
    chat = {
        "created": 1,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{\"a\":1}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    out = chat_to_responses_body(chat, "ds41f_cus")
    fn = [o for o in out["output"] if o["type"] == "function_call"][0]
    assert fn["call_id"] == "call_9"
    assert fn["name"] == "f"
    assert fn["arguments"] == "{\"a\":1}"


# -------------------------------------------------------------- streaming


def _sse_bytes(chunks: list[dict]) -> list[bytes]:
    out = []
    for c in chunks:
        out.append(f"data: {json.dumps(c)}\n\n".encode())
    out.append(b"data: [DONE]\n\n")
    return out


async def _collect_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    async def gen():
        for c in chunks:
            yield c

    events = []
    buffer = b""
    async for piece in stream_responses_events(gen(), "ds41f_cus"):
        buffer += piece
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            name = ""
            data = {}
            for line in block.split(b"\n"):
                if line.startswith(b"event: "):
                    name = line[7:].decode()
                elif line.startswith(b"data: "):
                    data = json.loads(line[6:])
            events.append((name, data))
    return events


@pytest.mark.asyncio
async def test_stream_event_sequence_text_only():
    chunks = _sse_bytes(
        [
            {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            },
        ]
    )
    events = await _collect_events(chunks)
    names = [n for n, _ in events]
    assert names[0] == "response.created"
    assert "response.output_text.delta" in names
    assert "response.output_item.done" in names
    assert names[-1] == "response.completed"

    completed = dict(events[-1][1])["response"]
    assert completed["model"] == "ds41f_cus"
    assert completed["usage"]["output_tokens"] == 2
    text_item = [o for o in completed["output"] if o["type"] == "message"][0]
    assert text_item["content"][0]["text"] == "hello"
    # deltas concatenate to the final text
    deltas = [d["delta"] for n, d in events if n == "response.output_text.delta"]
    assert "".join(deltas) == "hello"


@pytest.mark.asyncio
async def test_stream_event_sequence_tool_call():
    chunks = _sse_bytes(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "f", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "{\"a\":1}"}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = await _collect_events(chunks)
    names = [n for n, _ in events]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    completed = events[-1][1]["response"]
    fn = [o for o in completed["output"] if o["type"] == "function_call"][0]
    assert fn["arguments"] == "{\"a\":1}"
    assert fn["call_id"] == "call_1"
    assert "big-pickle" not in json.dumps(events)


# ------------------------------------------------------------ HTTP layer


class SseFakeAdapter:
    name = "zen-direct"

    def __init__(self):
        self.last_payload = None

    async def list_models(self):
        return {"object": "list", "data": []}

    async def chat(self, payload):
        self.last_payload = payload
        chunks = [
            {"choices": [{"delta": {"content": "hi there"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]

        async def relay_gen():
            for c in chunks:
                yield f"data: {json.dumps(c)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamRelay(
            httpx.Response(200, content=relay_gen()), {}
        )


@pytest.mark.asyncio
async def test_responses_endpoint_streams_masked_events(tmp_path):
    from proxy_opencode.app import create_app
    from proxy_opencode.config import Settings

    app = create_app(
        Settings(
            upstream_mode="zen-direct",
            gateway_api_keys=["gw-key"],
            key_store_path=str(tmp_path / "keys.json"),
        )
    )
    app.state.adapter = SseFakeAdapter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/responses",
            json={"model": "ds41f_cus", "input": "hello", "stream": True},
            headers={"Authorization": "Bearer gw-key"},
        )
    assert resp.status_code == 200
    assert "response.completed" in resp.text
    assert "big-pickle" not in resp.text
    # The adapter must have received the upstream model id.
    assert app.state.adapter.last_payload["model"] == "opencode/big-pickle"


@pytest.mark.asyncio
async def test_responses_endpoint_non_stream(tmp_path):
    from proxy_opencode.app import create_app
    from proxy_opencode.config import Settings
    from tests.test_gateway_routes import FakeAdapter

    app = create_app(
        Settings(
            upstream_mode="opencode-serve",
            gateway_api_keys=["gw-key"],
            key_store_path=str(tmp_path / "keys.json"),
            opencode_server_password="pw",
        )
    )
    app.state.adapter = FakeAdapter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/responses",
            json={"model": "ds41f_cus", "input": "hello"},
            headers={"Authorization": "Bearer gw-key"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["model"] == "ds41f_cus"
    assert body["output"][-1]["content"][0]["text"] == "pong"


# ------------------------------------------------- unpaired tool / empty input

def test_orphan_tool_message_degrades_to_user():
    """Regression: compressed-history replay can contain unpaired tool turns;
    relaying them verbatim made upstream 400 (invalid_request_error)."""
    payload = to_chat_payload(
        {
            "model": "ds41f_cus",
            "input": [
                {"type": "message", "role": "user", "content": "run ls"},
                {"type": "message", "role": "tool", "content": "file1.txt"},
            ],
        }
    )
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "user"]
    assert payload["messages"][1]["content"] == "[tool result] file1.txt"


def test_tool_message_with_call_id_stays_tool():
    payload = to_chat_payload(
        {
            "model": "ds41f_cus",
            "input": [
                {"type": "function_call", "call_id": "call_9",
                 "name": "t", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_9",
                 "output": "ok"},
                {"type": "message", "role": "tool", "tool_call_id": "call_9",
                 "content": "ok too"},
            ],
        }
    )
    tool_msgs = [m for m in payload["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert all(m["tool_call_id"] == "call_9" for m in tool_msgs)


def test_reasoning_only_input_never_yields_empty_messages():
    """Regression: all-skippable input produced messages: [] and upstream
    rejected the request outright."""
    payload = to_chat_payload(
        {
            "model": "ds41f_cus",
            "input": [{"type": "reasoning", "summary": [{"text": "hmm"}]}],
        }
    )
    assert payload["messages"]
    assert payload["messages"][-1]["role"] == "user"
