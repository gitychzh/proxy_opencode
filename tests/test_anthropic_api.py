"""Tests for the Anthropic Messages protocol conversion (/v1/messages)."""

from __future__ import annotations

import json

import httpx
import pytest

from proxy_opencode.formats.anthropic_proto import (
    chat_to_anthropic_body,
    stream_anthropic_events,
    to_chat_payload,
)

# --------------------------------------------------------------- request


def test_system_and_string_messages():
    payload = to_chat_payload(
        {
            "system": "be brief",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            "max_tokens": 100,
        }
    )
    assert payload["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert payload["max_tokens"] == 100


def test_content_blocks_with_tool_roundtrip():
    payload = to_chat_payload(
        {
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "what time?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "checking"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "clock",
                            "input": {"tz": "utc"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "12:00",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "clock",
                    "description": "clock",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
    )
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert payload["messages"][1]["tool_calls"][0]["id"] == "toolu_1"
    args = payload["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"tz": "utc"}
    assert payload["messages"][2]["content"] == "12:00"
    assert payload["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_empty_assistant_turn_is_dropped():
    """Regression: an assistant turn whose only part is internal reasoning (or
    an empty content list) used to become {"role":"assistant","content":null},
    which the chat upstream rejects with HTTP 400."""
    payload = to_chat_payload(
        {
            "model": "ds41f_cus",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
                {"role": "assistant", "content": []},
            ],
        }
    )
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_all_empty_turns_rejected_as_client_error():
    with pytest.raises(ValueError):
        to_chat_payload(
            {"model": "m", "max_tokens": 16, "messages": [{"role": "assistant", "content": []}]}
        )


# -------------------------------------------------------------- response


def test_chat_to_anthropic_body_text():
    chat = {
        "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    out = chat_to_anthropic_body(chat, "ds41f_cus")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "ds41f_cus"
    assert out["content"] == [{"type": "text", "text": "pong"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_chat_to_anthropic_body_tool_use():
    chat = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "clock", "arguments": "{\"tz\":\"utc\"}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    out = chat_to_anthropic_body(chat, "ds41f_cus")
    assert out["stop_reason"] == "tool_use"
    tool = out["content"][0]
    assert tool["type"] == "tool_use"
    assert tool["id"] == "call_1"
    assert tool["input"] == {"tz": "utc"}


# -------------------------------------------------------------- streaming


def _sse_bytes(chunks: list[dict]) -> list[bytes]:
    return [
        *(f"data: {json.dumps(c)}\n\n".encode() for c in chunks),
        b"data: [DONE]\n\n",
    ]


async def _collect_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    async def gen():
        for c in chunks:
            yield c

    events = []
    buffer = b""
    async for piece in stream_anthropic_events(gen(), "ds41f_cus"):
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
async def test_stream_anthropic_text_sequence():
    chunks = _sse_bytes(
        [
            {"choices": [{"delta": {"content": "he"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "y"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        ]
    )
    events = await _collect_events(chunks)
    names = [n for n, _ in events]
    assert names[0] == "message_start"
    assert names.count("content_block_delta") == 2
    assert names[-1] == "message_stop"
    # strict block lifecycle: start comes before its deltas, stop after
    assert names.index("content_block_start") < names.index("content_block_delta")
    assert names.index("content_block_stop") > names.index("content_block_delta")
    # message_start carries a well-formed message skeleton (SDK requirement)
    msg = events[0][1]["message"]
    assert msg["type"] == "message" and msg["role"] == "assistant"
    delta_ev = [d for n, d in events if n == "message_delta"][0]
    assert delta_ev["delta"]["stop_reason"] == "end_turn"
    assert delta_ev["usage"]["output_tokens"] == 2
    assert "big-pickle" not in json.dumps(events)


@pytest.mark.asyncio
async def test_stream_anthropic_tool_use_block():
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
                                    "function": {"name": "clock", "arguments": ""},
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
                                {"index": 0, "function": {"arguments": "{\"tz\""}}
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
                                {"index": 0, "function": {"arguments": ":\"utc\"}"}}
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
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    start = [d for n, d in events if n == "content_block_start"][0]
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "clock"
    deltas = [d["delta"]["partial_json"] for n, d in events if n == "content_block_delta"]
    assert "".join(deltas) == "{\"tz\":\"utc\"}"
    delta_ev = [d for n, d in events if n == "message_delta"][0]
    assert delta_ev["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_content_block_stop_carries_only_index():
    """The Anthropic spec puts just type+index on content_block_stop."""
    chunks = _sse_bytes(
        [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )
    events = await _collect_events(chunks)
    stops = [d for n, d in events if n == "content_block_stop"]
    assert stops
    for stop in stops:
        assert set(stop.keys()) == {"type", "index"}


# ------------------------------------------------------------ HTTP layer


@pytest.mark.asyncio
async def test_messages_endpoint_non_stream_and_x_api_key(tmp_path):
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
            "/v1/messages",
            json={
                "model": "ds41f_cus",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"x-api-key": "gw-key", "anthropic-version": "2023-06-01"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "message"
        assert body["model"] == "ds41f_cus"
        text_block = next(b for b in body["content"] if b["type"] == "text")
        assert text_block["text"] == "pong"

        # Wrong key -> Anthropic-shaped 401.
        r2 = await client.post(
            "/v1/messages",
            json={"model": "m", "max_tokens": 1, "messages": []},
            headers={"x-api-key": "nope"},
        )
        assert r2.status_code == 401
        assert r2.json()["type"] == "error"
        # The full key value must never leak in the error body.
        assert "nope" not in r2.text


@pytest.mark.asyncio
async def test_serve_completion_is_logged_exactly_once(tmp_path, caplog):
    """Regression: the serve-mode branch logged the same completion twice."""
    import logging

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
    with caplog.at_level(logging.INFO, logger="proxy_opencode.anthropic"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                json={"model": "ds41f_cus", "max_tokens": 32,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"x-api-key": "gw-key"},
            )
    assert resp.status_code == 200
    completions = [
        r for r in caplog.records
        if r.getMessage() == "anthropic messages completion"
    ]
    assert len(completions) == 1

