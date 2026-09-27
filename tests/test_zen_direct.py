"""Tests for the zen-direct adapter (protocol reconstruction)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from proxy_opencode.config import Settings
from proxy_opencode.upstreams import zen_direct
from proxy_opencode.upstreams.openai_http import StreamRelay
from proxy_opencode.upstreams.zen_direct import ZenDirectAdapter


def make_settings(**kw: Any) -> Settings:
    s = Settings(upstream_mode="zen-direct", **kw)
    return s


# --------------------------------------------------------------------- ids


def test_opencode_id_format_matches_opencode_schema() -> None:
    sid = zen_direct.opencode_id("ses_")
    assert sid.startswith("ses_")
    body = sid[4:]
    assert len(body) == 26
    int(body[:12], 16)  # time part is hex
    assert all(c.isalnum() for c in body[12:])
    assert zen_direct.is_valid_opencode_id(sid, "ses_")
    assert zen_direct.is_valid_opencode_id("msg_0da069dc1001mKuBwHfaKkaZSM", "msg_")
    # captured-fake id from the 2026-09 probe that got FreeTierError
    assert not zen_direct.is_valid_opencode_id("ses_ztest000001aaaaaaaaaaaaa", "ses_")


def test_opencode_id_time_part_matches_db_pairs() -> None:
    # Pairs taken from the local opencode.db (id prefix vs time.created ms):
    # time part == ((ms << 12) + counter) & 0xFFFFFFFFFFFF, counter starts at 1.
    pairs = [
        ("0e198de83001", 1790491287171),
        ("0e18e1c44001", 1790490582084),
        ("0e17f517e001", 1790489612670),
    ]
    for time_part, created_ms in pairs:
        assert int(time_part, 16) == ((created_ms << 12) + 1) & 0xFFFFFFFFFFFF


# ----------------------------------------------------------------- headers


@pytest.mark.asyncio
async def test_headers_reconstruct_captured_protocol() -> None:
    adapter = ZenDirectAdapter(make_settings())
    headers = adapter._headers()
    assert headers["Authorization"] == "Bearer public"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == (
        "opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
    )
    assert headers["x-opencode-client"] == "cli"
    assert headers["x-opencode-project"] == "global"
    assert zen_direct.is_valid_opencode_id(headers["x-opencode-session"], "ses_")
    assert zen_direct.is_valid_opencode_id(headers["x-opencode-request"], "msg_")
    await adapter.aclose()


@pytest.mark.asyncio
async def test_headers_use_api_key_when_configured() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    assert adapter._headers()["Authorization"] == "Bearer oc_sk_test"
    await adapter.aclose()


# -------------------------------------------------------------- body build


@pytest.mark.asyncio
async def test_body_injects_marker_and_merges_builtin_tools() -> None:
    adapter = ZenDirectAdapter(make_settings())
    body = adapter._build_body(
        {
            "model": "opencode/big-pickle",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
        },
        "big-pickle",
    )
    assert body["model"] == "big-pickle"
    assert body["temperature"] == 0.5
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith("You are opencode, an interactive CLI tool")
    assert "NEVER call the opencode built-in tools" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}
    # free tier gate: builtin tool schemas must be present
    names = {t["function"]["name"] for t in body["tools"]}
    assert {"bash", "read", "edit", "glob", "grep"} <= names
    assert body["tool_choice"] == "auto"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_merges_client_tools_after_builtins() -> None:
    adapter = ZenDirectAdapter(make_settings())
    custom = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": custom,
        },
        "big-pickle",
    )
    names = [t["function"]["name"] for t in body["tools"]]
    # builtin schemas first, client tools appended after
    assert names[0] == "bash"
    assert names[-1] == "get_weather"
    assert "get_weather" in names
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_client_tool_wins_name_collision() -> None:
    adapter = ZenDirectAdapter(make_settings())
    custom = [
        {
            "type": "function",
            "function": {
                "name": "bash",  # collides with a builtin
                "description": "client-side bash",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "x"}], "tools": custom},
        "big-pickle",
    )
    bash_defs = [t for t in body["tools"] if t["function"]["name"] == "bash"]
    assert len(bash_defs) == 1
    assert bash_defs[0]["function"]["description"] == "client-side bash"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_stream_options_mirrors_genuine_client() -> None:
    adapter = ZenDirectAdapter(make_settings())
    body = adapter._build_body(
        {"model": "big-pickle", "stream": True, "messages": [{"role": "user", "content": "x"}]},
        "big-pickle",
    )
    assert body["stream_options"] == {"include_usage": True}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_no_marker_with_api_key() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [{"role": "user", "content": "hi"}],
        },
        "big-pickle",
    )
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_strips_extra_message_fields() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "internal",
                    "tool_calls": [{"id": "c1", "type": "function"}],
                    "metadata": {"x": 1},
                }
            ],
        },
        "big-pickle",
    )
    msg = body["messages"][0]
    assert msg == {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function"}],
    }
    await adapter.aclose()


# --------------------------------------------------------------- chat flow


@respx.mock
@pytest.mark.asyncio
async def test_chat_non_stream_aggregates_sse_and_error_relay() -> None:
    route = respx.post("https://opencode.ai/zen/v1/chat/completions")
    adapter = ZenDirectAdapter(make_settings())

    # Free tier only serves stream=true; non-stream requests are aggregated.
    sse = (
        'data: {"id":"z1","created":123,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{"role":"assistant","reasoning_content":"thinking"}}]}\n\n'
        'data: {"id":"z1","created":123,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{"content":"po"}},{"index":0,"delta":{"content":"ng"}}]}\n\n'
        'data: {"id":"z1","created":123,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,'
        '"completion_tokens":2,"total_tokens":12}}\n\n'
        'data: [DONE]\n\n'
        'data: {"choices":[],"cost":"0"}\n\n'
    )
    route.respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=sse.encode(),
    )
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    msg = result["choices"][0]["message"]
    assert msg["content"] == "pong"
    assert msg["reasoning_content"] == "thinking"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 12
    assert result["id"] == "z1"
    assert result["metadata"]["aggregated_stream"] is True
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["model"] == "big-pickle"
    assert body["stream"] is True  # forced upstream
    assert body["stream_options"] == {"include_usage": True}
    assert sent.headers["Authorization"] == "Bearer public"
    assert sent.headers["x-opencode-client"] == "cli"

    route.respond(status_code=429, json={"error": {"message": "Rate limit exceeded."}})
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 429
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_stream_returns_relay() -> None:
    adapter = ZenDirectAdapter(make_settings())
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
    )
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": "q"}],
        }
    )
    assert isinstance(result, StreamRelay)
    chunks = b"".join([c async for c in result.chunks()])
    assert b"[DONE]" in chunks
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_models_endpoint() -> None:
    adapter = ZenDirectAdapter(make_settings())
    respx.get("https://opencode.ai/zen/v1/models").respond(
        json={"data": [{"id": "big-pickle"}, {"id": "qwen3-coder"}]}
    )
    result = await adapter.list_models()
    ids = [m["id"] for m in result["data"]]
    assert "opencode/big-pickle" in ids
    assert "opencode/qwen3-coder" in ids
    assert result["metadata"]["adapter"] == "zen-direct"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rejects_non_zen_host() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        ZenDirectAdapter(make_settings(zen_base_url="https://evil.example.com/v1"))


def test_settings_roundtrip() -> None:
    import os

    os.environ["UPSTREAM_MODE"] = "zen-direct"
    os.environ["OPENCODE_ZEN_API_KEY"] = "oc_sk_x"
    os.environ["ZEN_PROXY"] = "http://127.0.0.1:7897"
    try:
        from proxy_opencode.config import load_settings

        s = load_settings()
        assert s.is_zen_direct
        assert s.zen_api_key == "oc_sk_x"
        assert s.zen_proxy == "http://127.0.0.1:7897"
    finally:
        del os.environ["UPSTREAM_MODE"]
        del os.environ["OPENCODE_ZEN_API_KEY"]
        del os.environ["ZEN_PROXY"]


def test_httpx_proxy_acceptance() -> None:
    # httpx accepts the proxy form we document; keeps config honest.
    client = httpx.Client(proxy="http://127.0.0.1:7897")
    client.close()
