"""ServeAdapter against a mocked opencode serve (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from proxy_opencode.config import Settings
from proxy_opencode.upstreams.opencode_serve import ServeAdapter, ServeError

BASE = "http://127.0.0.1:4096"


def make_settings(**overrides) -> Settings:
    base = dict(
        opencode_serve_url=BASE,
        opencode_server_password="pw",
        opencode_serve_wait_timeout_s=30,
    )
    base.update(overrides)
    return Settings(**base)


def _session_payload(sid="ses_test"):
    return {"data": {"id": sid}}


def _assistant_message(text="4", extra_parts=None):
    content = [{"type": "text", "text": text}]
    if extra_parts:
        content = extra_parts + content
    return {
        "data": [
            {
                "id": "msg_asst",
                "type": "assistant",
                "finish": "stop",
                "model": {"id": "big-pickle", "providerID": "opencode"},
                "content": content,
                "tokens": {
                    "input": 100,
                    "output": 10,
                    "reasoning": 5,
                    "cache": {"read": 20, "write": 0},
                },
            },
            {"id": "msg_user", "type": "user", "text": "2+2?"},
        ]
    }


@pytest.mark.asyncio
async def test_full_turn_with_reasoning():
    settings = make_settings()
    adapter = ServeAdapter(settings)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/session").respond(json=_session_payload())
        mock.post("/api/session/ses_test/prompt").respond(json={"data": {}})
        mock.get("/api/session/ses_test/message").respond(
            json=_assistant_message(
                "4",
                extra_parts=[{"type": "reasoning", "text": "2+2=4"}],
            )
        )
        mock.delete("/session/ses_test").respond(json={"data": True})
        result = await adapter.chat(
            {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "2+2?"}]}
        )
    assert result.text == "4"
    assert result.reasoning == "2+2=4"
    assert result.usage["prompt_tokens"] == 120  # input + cache read
    assert result.usage["completion_tokens_details"]["reasoning_tokens"] == 5
    assert result.tool_calls == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_tool_contract_parse():
    settings = make_settings()
    adapter = ServeAdapter(settings)
    reply = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Paris"}}]}'
    captured = {}
    def _capture(request):
        import json as _json
        captured['body'] = _json.loads(request.content)
        return httpx.Response(200, json={'data': {}})
    with respx.mock(base_url=BASE) as mock:
        mock.post('/api/session').respond(json=_session_payload())
        mock.post('/api/session/ses_test/prompt').mock(side_effect=_capture)
        mock.get("/api/session/ses_test/message").respond(
            json=_assistant_message(reply)
        )
        mock.delete("/session/ses_test").respond(json={"data": True})
        result = await adapter.chat(
            {
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "parameters": {}},
                    }
                ],
            }
        )
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "get_weather"
    assert result.text == ""
    assert 'TOOLS' in captured['body']['prompt']['text']
    await adapter.aclose()


@pytest.mark.asyncio
async def test_waits_for_finished_assistant_message():
    settings = make_settings()
    adapter = ServeAdapter(settings)
    not_done = {"data": [{"id": "m1", "type": "user", "text": "hi"},
                         {"id": "m2", "type": "assistant", "finish": None,
                          "content": []}]}
    done = _assistant_message("done")
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/session").respond(json=_session_payload())
        mock.post("/api/session/ses_test/prompt").respond(json={"data": {}})
        mock.get("/api/session/ses_test/message").mock(
            side_effect=[httpx.Response(200, json=not_done),
                         httpx.Response(200, json=done),
                         httpx.Response(200, json=done)]
        )
        mock.delete("/session/ses_test").respond(json={"data": True})
        result = await adapter.chat(
            {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "hi"}]}
        )
    assert result.text == "done"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_bad_model_rejected():
    settings = make_settings()
    adapter = ServeAdapter(settings)
    with pytest.raises(ServeError):
        await adapter.chat(
            {"model": "not-a-provider-model", "messages": [{"role": "user", "content": "hi"}]}
        )
    await adapter.aclose()


@pytest.mark.asyncio
async def test_no_finished_assistant_is_error():
    settings = make_settings(opencode_serve_wait_timeout_s=2)
    adapter = ServeAdapter(settings)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/session").respond(json=_session_payload())
        mock.post("/api/session/ses_test/prompt").respond(json={"data": {}})
        mock.get("/api/session/ses_test/message").respond(
            json={"data": [{"id": "m1", "type": "user", "text": "hi"}]}
        )
        mock.delete("/session/ses_test").respond(json={"data": True})
        from proxy_opencode.upstreams.opencode_serve import WaitTimeoutError

        with pytest.raises(WaitTimeoutError):
            await adapter.chat(
                {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "hi"}]}
            )
    await adapter.aclose()

