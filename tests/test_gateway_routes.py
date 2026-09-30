"""HTTP-layer tests for routes: auth, chat, models, streaming.

Rate limiting was removed on 2026-09-30 (v0.6.1): the Zen upstream applies
its own scheduling, and a self-hosted per-key RPM limiter only penalised
agent fan-out. test_no_rate_limit_burst guards the removal.
"""

from __future__ import annotations

import json

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
async def test_no_rate_limit_burst():
    """v0.6.1 removed the per-key RPM limiter: rapid bursts must never 429."""
    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "hi"}]}
        codes = [
            (await client.post("/v1/chat/completions", json=body, headers=GW_HEADERS)).status_code
            for _ in range(5)
        ]
    assert codes == [200] * 5


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
    data = resp.json()["data"]
    # Model masking: only the public alias is ever listed.
    assert data[0]["id"] == "ds41f_cus"
    assert all(m["id"] != "opencode/big-pickle" for m in data)


# ------------------------------------------------- payload dump (debug)


@pytest.mark.asyncio
async def test_payload_dump_disabled_by_default(tmp_path):
    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "opencode/big-pickle",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=GW_HEADERS,
        )
    assert not any(tmp_path.rglob("*_chat.json"))


@pytest.mark.asyncio
async def test_payload_dump_writes_raw_body(tmp_path):
    app, _ = make_app(payload_dump_dir=str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1/chat/completions",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"junk": "stripped-later-by-whitelist"},
            },
            headers=GW_HEADERS,
        )
    dumps = list(tmp_path.glob("*_chat.json"))
    assert len(dumps) == 1
    raw = json.loads(dumps[0].read_text(encoding="utf-8"))
    # the raw body is dumped BEFORE whitelist filtering: the junk field survives
    assert raw["metadata"] == {"junk": "stripped-later-by-whitelist"}


@pytest.mark.asyncio
async def test_payload_dump_covers_all_three_protocols(tmp_path):
    """The audit must see claude code / codex traffic too, not just chat."""
    app, _ = make_app(payload_dump_dir=str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            headers=GW_HEADERS,
        )
        await client.post(
            "/v1/responses",
            json={"model": "m", "input": "hi"},
            headers=GW_HEADERS,
        )
        await client.post(
            "/v1/messages",
            json={"model": "m", "max_tokens": 16,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=GW_HEADERS,
        )
    slugs = sorted(p.name.rsplit("_", 1)[-1] for p in tmp_path.glob("*.json"))
    assert slugs == ["chat.json", "messages.json", "responses.json"]
