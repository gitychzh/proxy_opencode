"""Tests for the opencode-cli upstream mode (local, official CLI, restricted).

A fake ``opencode`` CLI is provided as a Python script injected via
``OPENCODE_BIN="<python> <script>"``; no real opencode installation or
network access is involved.
"""

from __future__ import annotations

import json
import shlex
import sys

import httpx
import pytest

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings, load_settings

GATEWAY_KEY = "gw-cli-key"
AUTH = {"Authorization": f"Bearer {GATEWAY_KEY}"}
CHAT_URL = "/v1/chat/completions"

FAKE_CLI = """
import sys

args = sys.argv[1:]
if args[:1] == ["models"]:
    print("opencode/big-pickle")
    print("opencode/zen")
    print("other/private-model")
elif args[:1] == ["run"]:
    print("fake cli reply")
else:
    sys.exit(3)
""".lstrip()

FAIL_CLI = """
import sys
print("boom", file=sys.stderr)
sys.exit(2)
""".lstrip()


def _write_cli(tmp_path, body: str = FAKE_CLI) -> str:
    script = tmp_path / "fake_opencode.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    # OPENCODE_BIN may contain args; use the current interpreter + script.
    return shlex.join([sys.executable, str(script)])


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        upstream_mode="opencode-cli",
        gateway_api_keys=[GATEWAY_KEY],
        opencode_bin=_write_cli(tmp_path),
        opencode_models_cmd="opencode models",
        opencode_allowed_model_prefixes=["opencode/"],
        models_cache_ttl_s=300,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
async def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        yield c


def _chat(**overrides):
    body = {
        "model": "opencode/big-pickle",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


async def test_models_filtered_and_cached(client):
    resp = await client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    ids = [m["id"] for m in data["data"]]
    assert ids == ["opencode/big-pickle", "opencode/zen"]
    assert data["metadata"]["adapter"] == "opencode-cli"


async def test_chat_non_stream(client):
    resp = await client.post(CHAT_URL, headers=AUTH, json=_chat())
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "fake cli reply\n"
    assert data["metadata"]["adapter"] == "opencode-cli"


async def test_chat_rejects_disallowed_model_prefix(client):
    resp = await client.post(CHAT_URL, headers=AUTH, json=_chat(model="gpt-4o"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_not_allowed"


async def test_chat_rejects_tools_and_reasoning(client):
    tools = [
        {
            "type": "function",
            "function": {"name": "get_weather", "parameters": {}},
        }
    ]
    for extra in (
        {"tools": tools},
        {"tool_calls": tools},
        {"response_format": {"type": "json_object"}},
        {"reasoning_effort": "high"},
    ):
        resp = await client.post(CHAT_URL, headers=AUTH, json=_chat(**extra))
        assert resp.status_code == 400, extra
        assert "not support tool/reasoning passthrough" in (
            resp.json()["error"]["message"]
        )


async def test_chat_synthetic_stream(client):
    resp = await client.post(CHAT_URL, headers=AUTH, json=_chat(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data:")]
    assert len(lines) == 2
    chunk = json.loads(lines[0][5:].strip())
    assert chunk["choices"][0]["delta"]["content"] == "fake cli reply\n"
    assert chunk["metadata"]["adapter"] == "opencode-cli"
    assert chunk["metadata"]["synthetic_stream"] is True
    assert lines[1] == "data: [DONE]"


async def test_cli_failure_returns_502(tmp_path):
    app = create_app(make_settings(tmp_path, opencode_bin=_write_cli(tmp_path / "x", FAIL_CLI)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        resp = await c.get("/v1/models", headers=AUTH)
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "api_error"
        resp = await c.post(CHAT_URL, headers=AUTH, json=_chat())
        assert resp.status_code == 502


async def test_cli_missing_binary_returns_502(tmp_path):
    app = create_app(
        make_settings(tmp_path, opencode_bin="definitely-not-a-real-opencode-bin")
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as c:
        resp = await c.get("/v1/models", headers=AUTH)
        assert resp.status_code == 502
        resp = await c.post(CHAT_URL, headers=AUTH, json=_chat())
        assert resp.status_code == 502


def test_load_settings_defaults(monkeypatch):
    for var in (
        "UPSTREAM_MODE",
        "OPENCODE_BIN",
        "OPENCODE_MODELS_CMD",
        "OPENCODE_RUN_TIMEOUT_S",
        "OPENCODE_XDG_DATA_HOME",
        "OPENCODE_ALLOWED_MODEL_PREFIXES",
        "MODELS_CACHE_TTL_S",
    ):
        monkeypatch.delenv(var, raising=False)
    s = load_settings()
    assert s.upstream_mode == "openai"
    assert s.opencode_bin == "opencode"
    assert s.opencode_models_cmd == "opencode models"
    assert s.opencode_run_timeout_s == 120
    assert s.opencode_xdg_data_home == ""
    assert s.opencode_allowed_model_prefixes == ["opencode/"]
    assert s.models_cache_ttl_s == 300
    assert not s.is_opencode_cli


def test_load_settings_rejects_bad_mode(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "something-else")
    with pytest.raises(ValueError):
        load_settings()
