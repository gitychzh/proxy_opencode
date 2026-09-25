"""HTTP-layer tests for routes: auth, rate limit, chat, models, streaming."""

from __future__ import annotations

import httpx
import pytest

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings
from proxy_opencode.upstreams.opencode_serve import ServeCompletion

GW_HEADERS = {"Authorization": "Bearer gw-key"}


class FakeAdapter:
    name = "opencode-serve"

    def __init__(self):
        self.last_payload = None

    async def list_models(self):
        return {"object": "list", "data": [{"id": "opencode/big-pickle", "object": "model"}]}

    async def chat(self, payload):
        self.last_payload = payload
        return ServeCompletion(
            text="pong",
            reasoning="thinking...",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            model_id="big-pickle",
        )


def make_app(**overrides):
    base = dict(
        upstream_mode="opencode-serve",
        gateway_api_keys=["gw-key"],
        opencode_server_password="pw",
    )
    base.update(overrides)
    settings = Settings(**base)
    app = create_app(settings)
    fake = FakeAdapter()
    app.state.adapter = fake
    return app, fake


@pytest.mark.asyncio
async def test_healthz():
    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["upstream_mode"] == "opencode-serve"


@pytest.mark.asyncio
async def test_auth_required():
    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/chat/completions", json={})
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_rate_limit():
    app, _ = make_app(requests_per_minute=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "hi"}]}
        r1 = await client.post("/v1/chat/completions", json=body, headers=GW_HEADERS)
        r2 = await client.post("/v1/chat/completions", json=body, headers=GW_HEADERS)
    assert r1.status_code == 200
    assert r2.status_code == 429


@pytest.mark.asyncio
async def test_chat_non_stream():
    app, fake = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "medium",
            },
            headers=GW_HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "pong"
    assert data["choices"][0]["message"]["reasoning_content"] == "thinking..."
    assert data["usage"]["total_tokens"] == 4
    assert data["metadata"]["adapter"] == "opencode-serve"
    assert fake.last_payload["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_chat_synthetic_stream():
    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=GW_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.headers["x-opencode-serve-synthetic-stream"] == "true"
    assert "reasoning_content" in resp.text
    assert "data: [DONE]" in resp.text


@pytest.mark.asyncio
async def test_models():
    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/models", headers=GW_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "opencode/big-pickle"
