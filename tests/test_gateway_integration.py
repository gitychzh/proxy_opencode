"""End-to-end integration tests: real uvicorn gateway -> fake upstream, all loopback.

No real upstream key is required. Both servers run on random free localhost
ports inside the pytest process.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings
from proxy_opencode.ratelimit import RateLimiter
from fake_upstream import DEFAULT_MODEL
from fake_upstream import create_app as create_fake_upstream

GATEWAY_KEY = "gw-test-key"
UPSTREAM_KEY = "upstream-dummy-key"
RL_KEY = "gw-rl-key"
CHAT_URL = "/v1/chat/completions"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RunningServer:
    def __init__(self, app, name: str) -> None:
        self.port = _free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name=name, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn server did not start in time")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class _LowLimitForRlKey(RateLimiter):
    """Give the dedicated rate-limit key a tiny limit so 429 is quick to hit."""

    def allow(self, key: str) -> bool:
        if key == RL_KEY:
            saved, self.limit = self.limit, 1
            try:
                return super().allow(key)
            finally:
                self.limit = saved
        return super().allow(key)


@pytest.fixture(scope="module")
def upstream_server():
    server = _RunningServer(create_fake_upstream(), "fake-upstream")
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def gateway_server(upstream_server):
    settings = Settings(
        upstream_base_url=upstream_server.base_url,
        upstream_api_key=UPSTREAM_KEY,
        gateway_api_keys=[GATEWAY_KEY, RL_KEY],
        requests_per_minute=60,
        reasoning_passthrough=True,
    )
    app = create_app(settings)
    app.state.ratelimiter = _LowLimitForRlKey(60)
    server = _RunningServer(app, "gateway")
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {GATEWAY_KEY}"}


def _chat_body(**overrides) -> dict[str, Any]:
    body = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def test_healthz(gateway_server):
    resp = httpx.get(f"{gateway_server.base_url}/healthz", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["upstream"].startswith("http://127.0.0.1:")
    assert data["dev_open"] is False


def test_models_forwarded_and_key_swapped(gateway_server, auth):
    resp = httpx.get(f"{gateway_server.base_url}/v1/models", headers=auth, timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["id"] == DEFAULT_MODEL
    echo = data["metadata"]["echo"]
    assert echo["authorization_present"] is True
    # The upstream must see UPSTREAM_API_KEY, never the client/gateway key.
    assert echo["authorization_sha256_12"] == hashlib.sha256(
        UPSTREAM_KEY.encode()
    ).hexdigest()[:12]


def test_chat_non_stream(gateway_server, auth):
    resp = httpx.post(
        f"{gateway_server.base_url}{CHAT_URL}",
        headers=auth,
        json=_chat_body(),
        timeout=5,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "fake upstream reply"
    assert data["usage"]["total_tokens"] == 5
    assert data["metadata"]["echo"]["stream"] is False


def test_chat_stream_sse(gateway_server, auth):
    body_lines: list[str] = []
    with httpx.stream(
        "POST",
        f"{gateway_server.base_url}{CHAT_URL}",
        headers=auth,
        json=_chat_body(stream=True),
        timeout=5,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line:
                body_lines.append(line)
    data_lines = [ln for ln in body_lines if ln.startswith("data:")]
    assert data_lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[5:].strip()) for ln in data_lines[:-1]]
    text = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert text == "fake reply"
    assert chunks[-1]["usage"]["total_tokens"] == 5
    assert chunks[-1]["metadata"]["echo"]["stream"] is True


def test_tools_and_reasoning_passthrough(gateway_server, auth):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    resp = httpx.post(
        f"{gateway_server.base_url}{CHAT_URL}",
        headers=auth,
        json=_chat_body(
            tools=tools,
            tool_choice="auto",
            reasoning_effort="high",
            reasoning={"effort": "high"},
        ),
        timeout=5,
    )
    assert resp.status_code == 200
    echo = resp.json()["metadata"]["echo"]
    assert echo["tools"] == tools
    assert echo["tool_choice"] == "auto"
    assert echo["reasoning"]["reasoning_effort"] == "high"
    assert echo["reasoning"]["reasoning"] == {"effort": "high"}


def test_gateway_401(gateway_server):
    for headers in ({}, {"Authorization": "Bearer wrong-key"}):
        resp = httpx.get(
            f"{gateway_server.base_url}/v1/models", headers=headers, timeout=5
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "authentication_error"
    resp = httpx.post(
        f"{gateway_server.base_url}{CHAT_URL}", json=_chat_body(), timeout=5
    )
    assert resp.status_code == 401


def test_gateway_429(gateway_server):
    headers = {"Authorization": f"Bearer {RL_KEY}"}
    url = f"{gateway_server.base_url}/v1/models"
    assert httpx.get(url, headers=headers, timeout=5).status_code == 200
    resp = httpx.get(url, headers=headers, timeout=5)
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"
