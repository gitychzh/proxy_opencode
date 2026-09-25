"""Smoke tests for proxy_opencode using httpx ASGITransport + respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings

UPSTREAM = "https://upstream.test"


def make_settings(**overrides) -> Settings:
    base = dict(
        upstream_base_url=UPSTREAM,
        upstream_api_key="upstream-secret",
        gateway_api_keys=["gw-key-1"],
        requests_per_minute=60,
        reasoning_passthrough=True,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        yield c
    await app.state.http_client.aclose()


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_required(client):
    # No key -> 401
    resp = await client.get("/v1/models")
    assert resp.status_code == 401
    # Wrong key -> 401
    resp = await client.get(
        "/v1/models", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_dev_open_when_no_keys():
    app = create_app(make_settings(gateway_api_keys=[]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        with respx.mock(base_url=UPSTREAM) as mock:
            mock.get("/v1/models").mock(
                return_value=httpx.Response(200, json={"object": "list", "data": []})
            )
            resp = await c.get("/v1/models")
            assert resp.status_code == 200
    await app.state.http_client.aclose()


@pytest.mark.asyncio
async def test_models_proxied_and_client_key_not_forwarded(client):
    with respx.mock(base_url=UPSTREAM) as mock:
        route = mock.get("/v1/models").mock(
            return_value=httpx.Response(
                200, json={"object": "list", "data": [{"id": "m1"}]}
            )
        )
        resp = await client.get(
            "/v1/models", headers={"Authorization": "Bearer gw-key-1"}
        )
        assert resp.status_code == 200
        sent = route.calls.last.request
        # Upstream key is used; the gateway key is never forwarded.
        assert sent.headers["authorization"] == "Bearer upstream-secret"


@pytest.mark.asyncio
async def test_chat_non_stream(client):
    upstream_body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    with respx.mock(base_url=UPSTREAM) as mock:
        route = mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=upstream_body)
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer gw-key-1"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == upstream_body
        sent_payload = json.loads(route.calls.last.request.content)
        assert sent_payload["model"] == "gpt-4o-mini"
        assert sent_payload.get("stream") is None


@pytest.mark.asyncio
async def test_tool_fields_passthrough(client):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with respx.mock(base_url=UPSTREAM) as mock:
        route = mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"id": "x", "choices": [], "usage": None}
            )
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer gw-key-1"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.5,
                "top_p": 0.9,
                "max_tokens": 128,
                "response_format": {"type": "json_object"},
                "reasoning_effort": "high",
                # Field not on the allowlist must be dropped.
                "prompt_logprobs": True,
            },
        )
        assert resp.status_code == 200
        sent = json.loads(route.calls.last.request.content)
        assert sent["tools"] == tools
        assert sent["tool_choice"] == "auto"
        assert sent["temperature"] == 0.5
        assert sent["top_p"] == 0.9
        assert sent["max_tokens"] == 128
        assert sent["response_format"] == {"type": "json_object"}
        # reasoning passthrough enabled in settings
        assert sent["reasoning_effort"] == "high"
        assert "prompt_logprobs" not in sent


@pytest.mark.asyncio
async def test_reasoning_passthrough_disabled():
    app = create_app(make_settings(reasoning_passthrough=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        with respx.mock(base_url=UPSTREAM) as mock:
            route = mock.post("/v1/chat/completions").mock(
                return_value=httpx.Response(200, json={"id": "x", "choices": []})
            )
            resp = await c.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer gw-key-1"},
                json={
                    "model": "m",
                    "messages": [{"role": "user", "content": "hi"}],
                    "reasoning_effort": "high",
                    "thinking": {"type": "enabled"},
                },
            )
            assert resp.status_code == 200
            sent = json.loads(route.calls.last.request.content)
            assert "reasoning_effort" not in sent
            assert "thinking" not in sent
    await app.state.http_client.aclose()


@pytest.mark.asyncio
async def test_chat_stream_sse(client):
    sse = (
        b'data: {"id":"c1","choices":[{"delta":{"content":"he"}}]}\n\n'
        b'data: {"id":"c1","choices":[{"delta":{"content":"llo"}}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )
    with respx.mock(base_url=UPSTREAM) as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse,
                headers={"content-type": "text/event-stream"},
            )
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer gw-key-1"},
            json={
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.content == sse


@pytest.mark.asyncio
async def test_upstream_error_relayed(client):
    err = {
        "error": {
            "message": "model not found",
            "type": "invalid_request_error",
            "param": None,
            "code": "model_not_found",
        }
    }
    with respx.mock(base_url=UPSTREAM) as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(404, json=err)
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer gw-key-1"},
            json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404
        assert resp.json() == err


@pytest.mark.asyncio
async def test_rate_limit():
    app = create_app(make_settings(requests_per_minute=2))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        with respx.mock(base_url=UPSTREAM) as mock:
            mock.get("/v1/models").mock(
                return_value=httpx.Response(200, json={"object": "list", "data": []})
            )
            headers = {"Authorization": "Bearer gw-key-1"}
            assert (await c.get("/v1/models", headers=headers)).status_code == 200
            assert (await c.get("/v1/models", headers=headers)).status_code == 200
            resp = await c.get("/v1/models", headers=headers)
            assert resp.status_code == 429
            assert resp.json()["error"]["type"] == "rate_limit_error"
    await app.state.http_client.aclose()
