"""Tests for the opencode-serve upstream mode (local, official `opencode serve` API).

A local FastAPI fake opencode-serve runs on a random 127.0.0.1 port (real HTTP,
Basic auth, /api routes). No real opencode installation or external network is
involved; CLI/openai modes are regression-checked via existing suites.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import Depends, FastAPI, Request

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings, load_settings

GATEWAY_KEY = "gw-serve-key"
AUTH = {"Authorization": f"Bearer {GATEWAY_KEY}"}
CHAT_URL = "/v1/chat/completions"
SERVE_PASSWORD = "serve-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RunningServer:
    def __init__(self, app, name: str) -> None:
        self.app = app
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


def create_fake_serve() -> FastAPI:
    """Minimal fake of the official `opencode serve` /api surface."""
    app = FastAPI()
    app.state.sessions: dict[str, dict[str, Any]] = {}
    app.state.seen_auth: list[str] = []
    app.state.seen_models: list[dict[str, str]] = []

    async def check_auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        if header:
            app.state.seen_auth.append(header)
        expected = "Basic " + base64.b64encode(
            f"opencode:{SERVE_PASSWORD}".encode()
        ).decode()
        if header != expected:
            raise _Http401()

    from fastapi import HTTPException

    class _Http401(HTTPException):
        def __init__(self) -> None:
            super().__init__(status_code=401, detail="unauthorized")

    @app.post("/api/session", dependencies=[Depends(check_auth)])
    async def create_session(request: Request) -> dict[str, Any]:
        body = await request.json()
        sid = f"sess-{len(app.state.sessions) + 1}"
        app.state.sessions[sid] = {"model": body.get("model"), "prompts": []}
        return {"data": {"id": sid}}

    @app.post("/api/session/{sid}/prompt", dependencies=[Depends(check_auth)])
    async def prompt(sid: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        app.state.sessions[sid]["prompts"].append(body)
        app.state.seen_models.append(body.get("model") or {})
        return {"data": {"admitted": True}}

    @app.post("/api/session/{sid}/wait", dependencies=[Depends(check_auth)])
    async def wait(sid: str) -> dict[str, Any]:
        return {"data": {"status": "idle"}}

    @app.get("/api/session/{sid}/message", dependencies=[Depends(check_auth)])
    async def message(sid: str) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "msg-a1",
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "thinking out loud"},
                        {"type": "text", "text": "hello "},
                        {"type": "text", "text": "from serve"},
                        {
                            "type": "tool",
                            "id": "tool-1",
                            "name": "read_file",
                            "input": {"path": "/tmp/a"},
                            "output": "file contents",
                        },
                    ],
                },
                {"id": "msg-u1", "role": "user", "content": [{"type": "text"}]},
            ]
        }

    return app


@pytest.fixture(scope="module")
def serve_server():
    server = _RunningServer(create_fake_serve(), "fake-opencode-serve")
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def gateway_server(serve_server):
    settings = Settings(
        upstream_mode="opencode-serve",
        gateway_api_keys=[GATEWAY_KEY],
        opencode_serve_url=serve_server.base_url,
        opencode_server_username="opencode",
        opencode_server_password=SERVE_PASSWORD,
        opencode_serve_models=["opencode/big-pickle", "opencode/zen"],
        opencode_serve_timeout_s=10,
        opencode_serve_wait_timeout_s=10,
    )
    server = _RunningServer(create_app(settings), "gateway-serve")
    server.start()
    yield server
    server.stop()


def _chat(client_url: str, **overrides):
    body = {
        "model": "opencode/big-pickle",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ],
    }
    body.update(overrides)
    return httpx.post(
        f"{client_url}{CHAT_URL}", headers=AUTH, json=body, timeout=15
    )


def test_models_static_list(gateway_server):
    resp = httpx.get(
        f"{gateway_server.base_url}/v1/models", headers=AUTH, timeout=5
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [m["id"] for m in data["data"]] == [
        "opencode/big-pickle",
        "opencode/zen",
    ]
    assert data["metadata"]["adapter"] == "opencode-serve"


def test_chat_non_stream_full_flow(gateway_server, serve_server):
    resp = _chat(gateway_server.base_url)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    msg = data["choices"][0]["message"]
    assert msg["content"] == "hello from serve"
    assert msg["reasoning_content"] == "thinking out loud"
    assert msg["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
        "path": "/tmp/a"
    }
    meta = data["metadata"]
    assert meta["adapter"] == "opencode-serve"
    assert meta["tools_source"] == "opencode-agent"
    # Basic auth reached the fake server with the configured password.
    expected = "Basic " + base64.b64encode(
        f"opencode:{SERVE_PASSWORD}".encode()
    ).decode()
    assert expected in serve_server.app.state.seen_auth
    # Model was split into provider/id and forwarded.
    assert serve_server.app.state.seen_models[-1] == {
        "id": "big-pickle",
        "providerID": "opencode",
    }


def test_wrong_password_502(serve_server):
    settings = Settings(
        upstream_mode="opencode-serve",
        opencode_serve_url=serve_server.base_url,
        opencode_server_password="wrong",
    )
    app = create_app(settings)  # dev-open: no gateway keys
    resp = httpx.post(
        f"{app.state.settings.opencode_serve_url}/api/session", timeout=5
    )
    assert resp.status_code == 401  # fake enforces auth
    _run_through_gateway(app, expect_status=502)


def _run_through_gateway(app, expect_status: int):
    settings = app.state.settings
    gw = _RunningServer(create_app(settings), f"gw-{id(app)}")
    gw.start()
    try:
        resp = httpx.post(
            f"{gw.base_url}{CHAT_URL}",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=15,
        )
        assert resp.status_code == expect_status
        assert resp.json()["error"]["type"] == "api_error"
    finally:
        gw.stop()


def test_unreachable_serve_502():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed on context exit -> connection refused
    settings = Settings(
        upstream_mode="opencode-serve",
        opencode_serve_url=f"http://127.0.0.1:{port}",
        opencode_server_password="x",
    )
    gw = _RunningServer(create_app(settings), "gw-unreachable")
    gw.start()
    try:
        resp = httpx.post(
            f"{gw.base_url}{CHAT_URL}",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=15,
        )
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "api_error"
    finally:
        gw.stop()


def test_client_tools_rejected_400(gateway_server):
    tools = [
        {
            "type": "function",
            "function": {"name": "f", "parameters": {}},
        }
    ]
    for extra in (
        {"tools": tools},
        {"tool_choice": "auto"},
        {"response_format": {"type": "json_object"}},
        {"reasoning_effort": "high"},
    ):
        resp = _chat(gateway_server.base_url, **extra)
        assert resp.status_code == 400, extra
        assert "unsupported fields" in resp.json()["error"]["message"]


def test_bad_model_format_400(gateway_server):
    resp = _chat(gateway_server.base_url, model="gpt-4o")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_model"


def test_synthetic_stream(gateway_server):
    resp = _chat(gateway_server.base_url, stream=True)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-opencode-serve-synthetic-stream"] == "true"
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data:")]
    assert len(lines) == 2
    chunk = json.loads(lines[0][5:].strip())
    assert chunk["choices"][0]["delta"]["content"] == "hello from serve"
    assert chunk["metadata"]["adapter"] == "opencode-serve"
    assert chunk["metadata"]["synthetic_stream"] is True
    assert lines[1] == "data: [DONE]"


def test_load_settings_non_loopback_rejected(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "opencode-serve")
    monkeypatch.setenv("OPENCODE_SERVE_URL", "http://192.168.1.10:4096")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    with pytest.raises(ValueError, match="loopback"):
        load_settings()


def test_load_settings_passwordless_requires_127001(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "opencode-serve")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "")
    monkeypatch.setenv("OPENCODE_SERVE_URL", "http://localhost:4096")
    with pytest.raises(ValueError, match="127.0.0.1"):
        load_settings()
    monkeypatch.setenv("OPENCODE_SERVE_URL", "http://127.0.0.1:4096")
    s = load_settings()
    assert s.is_opencode_serve
    assert not s.is_opencode_cli
    assert s.opencode_serve_url == "http://127.0.0.1:4096"
    assert s.opencode_server_username == "opencode"
    assert s.opencode_server_password == ""
    assert s.opencode_serve_models == ["opencode/big-pickle"]


def _retry_serve(statuses: list[int]) -> FastAPI:
    """Fake serve whose /wait returns `statuses[0]`, `statuses[1]`, ... then 200."""
    app = FastAPI()
    app.state.wait_calls: dict[str, int] = {}

    @app.post("/api/session")
    async def create_session(request: Request) -> dict[str, Any]:
        body = await request.json()
        return {"data": {"id": "sess-retry"}}

    @app.post("/api/session/{sid}/prompt")
    async def prompt(sid: str, request: Request) -> dict[str, Any]:
        return {"data": {"admitted": True}}

    @app.post("/api/session/{sid}/wait")
    async def wait(sid: str):
        from fastapi.responses import JSONResponse as _JR

        n = app.state.wait_calls.get(sid, 0)
        app.state.wait_calls[sid] = n + 1
        status = statuses[n] if n < len(statuses) else 200
        return _JR(status_code=status, content={"data": {"status": "busy"}})

    @app.get("/api/session/{sid}/message")
    async def message(sid: str) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "msg-r1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry reply"}],
                },
            ]
        }

    return app


def _serve_gateway(serve_app: FastAPI, **overrides) -> tuple:
    serve = _RunningServer(serve_app, "fake-serve-retry")
    serve.start()
    base = {
        "upstream_mode": "opencode-serve",
        "opencode_serve_url": serve.base_url,
        "opencode_serve_timeout_s": 15,
        "opencode_serve_wait_timeout_s": 15,
    }
    base.update(overrides)
    settings = Settings(**base)
    gw = _RunningServer(create_app(settings), "gw-serve-retry")
    gw.start()
    return serve, gw


def test_wait_retries_503_then_succeeds():
    serve, gw = _serve_gateway(_retry_serve([503, 503]))
    try:
        resp = httpx.post(
            f"{gw.base_url}{CHAT_URL}",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=30,
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "retry reply"
        assert serve.app.state.wait_calls["sess-retry"] == 3
    finally:
        gw.stop()
        serve.stop()


def test_wait_409_then_succeeds():
    serve, gw = _serve_gateway(_retry_serve([409]))
    try:
        resp = httpx.post(
            f"{gw.base_url}{CHAT_URL}",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=30,
        )
        assert resp.status_code == 200
        assert serve.app.state.wait_calls["sess-retry"] == 2
    finally:
        gw.stop()
        serve.stop()


def test_wait_stuck_503_returns_504():
    serve, gw = _serve_gateway(
        _retry_serve([503] * 100), opencode_serve_wait_timeout_s=1
    )
    try:
        resp = httpx.post(
            f"{gw.base_url}{CHAT_URL}",
            json={
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=30,
        )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "wait_timeout"
    finally:
        gw.stop()
        serve.stop()


def test_cli_and_openai_defaults_untouched(monkeypatch):
    for var in ("UPSTREAM_MODE", "OPENCODE_SERVE_URL"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings()
    assert s.upstream_mode == "openai"
    assert s.opencode_serve_url == "http://127.0.0.1:4096"
    monkeypatch.setenv("UPSTREAM_MODE", "opencode-cli")
    s = load_settings()
    assert s.is_opencode_cli
    assert not s.is_opencode_serve
