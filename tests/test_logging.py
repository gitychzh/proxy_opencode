"""Tests for structured logging (logsetup) and observability hooks."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from proxy_opencode import __version__
from proxy_opencode.logsetup import ExtraFormatter, JsonFormatter, setup_logging


def _record(**extras) -> logging.LogRecord:
    return logging.Logger("t").makeRecord(
        name="proxy_opencode.chat",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="chat completion",
        args=(),
        exc_info=None,
        extra=extras,
    )


def test_text_formatter_renders_whitelisted_extras():
    fmt = ExtraFormatter(fmt="%(message)s")
    out = fmt.format(_record(request_id="abc123", status=200, latency_ms=12.5))
    assert "chat completion" in out
    assert "request_id=abc123" in out
    assert "status=200" in out
    assert "latency_ms=12.5" in out


def test_formatters_ignore_unknown_extras():
    fmt = ExtraFormatter(fmt="%(message)s")
    out = fmt.format(_record(prompt="SECRET PROMPT", api_key="sk-secret"))
    assert "SECRET" not in out
    assert "sk-secret" not in out
    jout = JsonFormatter().format(_record(prompt="SECRET PROMPT"))
    assert "SECRET" not in jout


def test_json_formatter_outputs_parseable_line():
    out = JsonFormatter().format(
        _record(request_id="abc123", model="opencode/big-pickle", status=200)
    )
    parsed = json.loads(out)
    assert parsed["msg"] == "chat completion"
    assert parsed["request_id"] == "abc123"
    assert parsed["model"] == "opencode/big-pickle"
    assert parsed["level"] == "INFO"
    assert "ts" in parsed


def test_setup_logging_env_json(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging()
    root = logging.getLogger()
    json_handlers = [
        h for h in root.handlers if isinstance(h.formatter, JsonFormatter)
    ]
    assert json_handlers, "LOG_FORMAT=json should install the JSON formatter"
    monkeypatch.setenv("LOG_FORMAT", "text")
    setup_logging()
    text_handlers = [
        h for h in root.handlers if isinstance(h.formatter, ExtraFormatter)
    ]
    assert text_handlers, "LOG_FORMAT=text should install the text formatter"


@pytest.mark.asyncio
async def test_healthz_includes_version():
    from tests.test_gateway_routes import make_app

    app, _ = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")
    body = resp.json()
    assert resp.status_code == 200
    assert body["version"] == __version__
    assert body["version"] != "0.2.0", "version must come from installed metadata"


@pytest.mark.asyncio
async def test_auth_rejection_is_logged_without_token(caplog):
    from tests.test_gateway_routes import make_app

    app, _ = make_app()
    with caplog.at_level(logging.WARNING, logger="proxy_opencode.security"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={},
                headers={"Authorization": "Bearer super-secret-wrong-key"},
            )
    assert resp.status_code == 401
    assert any(r.getMessage() == "gateway auth rejected" for r in caplog.records)
    assert "super-secret-wrong-key" not in caplog.text, "token must never be logged"


@pytest.mark.asyncio
async def test_chat_unhandled_adapter_error_returns_500(caplog):
    from tests.test_gateway_routes import make_app

    app, fake = make_app()

    async def boom(payload):
        raise RuntimeError("adapter exploded")

    fake.chat = boom
    with caplog.at_level(logging.ERROR, logger="proxy_opencode.chat"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions", json={}, headers={"Authorization": "Bearer gw-key"}
            )
    assert resp.status_code == 500
    assert resp.json()["error"]["type"] == "api_error"
    assert any("unhandled chat error" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "traceback must be logged"
