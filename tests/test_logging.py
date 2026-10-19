"""Tests for structured logging (logsetup) and observability hooks."""

from __future__ import annotations

import asyncio
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


def test_key_lifecycle_fields_are_rendered():
    """Regression: key_id/ttl_hours/path were passed as `extra` but missing
    from the whitelist, so they vanished from every log line."""
    fmt = ExtraFormatter(fmt="%(message)s")
    out = fmt.format(
        _record(key_id="k-1", ttl_hours=24.0, key_name="phone", path="/tmp/keys.json")
    )
    for fragment in ("key_id=k-1", "ttl_hours=24.0", "key_name=phone", "path=/tmp/keys.json"):
        assert fragment in out
    parsed = json.loads(JsonFormatter().format(_record(key_id="k-1", shape="list")))
    assert parsed["key_id"] == "k-1"
    assert parsed["shape"] == "list"


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


def test_app_factory_configures_logging(monkeypatch):
    """The container starts via create_app_from_env (never `python -m
    proxy_opencode`), so THIS is the only place production ever runs
    setup_logging — without it the root logger had no handler, every INFO
    record was dropped and WARNINGs lost their extras (found 2026-10-19
    while attributing a stream drop to a bucket).
    """
    import proxy_opencode.app as app_mod

    calls: list[int] = []
    sentinel_settings = object()
    monkeypatch.setattr(app_mod, "setup_logging", lambda: calls.append(1))
    monkeypatch.setattr(app_mod, "load_settings", lambda: sentinel_settings)
    monkeypatch.setattr(app_mod, "create_app", lambda settings: "app-sentinel")
    assert app_mod.create_app_from_env() == "app-sentinel"
    assert calls == [1], "create_app_from_env must configure logging first"


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
    with caplog.at_level(logging.WARNING, logger="proxy_opencode.auth"):
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
                "/v1/chat/completions",
                json={
                    "model": "deepseek-ai/deepseek-v4.1-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": "Bearer gw-key"},
            )
    assert resp.status_code == 500
    assert resp.json()["error"]["type"] == "api_error"
    assert any("unhandled chat error" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "traceback must be logged"


class FakeStreamRelay:
    """Minimal stand-in for StreamRelay: replays chunks, optional failure."""

    def __init__(self, chunks=(b"a", b"b"), exc=None):
        self._chunks = list(chunks)
        self._exc = exc
        self.usage_holder: dict = {}

    async def chunks(self):
        for c in self._chunks:
            if self._exc is not None and c == self._chunks[-1]:
                raise self._exc
            yield c


async def _collect_relay(relay):
    from proxy_opencode.routes._pipeline import relay_stream

    out = []
    async for chunk in relay_stream(
        relay.chunks(), relay.usage_holder, lambda status, usage=None: None,
        request_id="req1", model="m", client="127.0.0.1",
    ):
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_stream_relay_logs_completed(caplog):
    with caplog.at_level(logging.INFO, logger="proxy_opencode.pipeline"):
        out = await _collect_relay(FakeStreamRelay())
    assert out == [b"a", b"b"]
    ended = [r for r in caplog.records if r.getMessage() == "stream ended"]
    assert len(ended) == 1
    assert ended[0].chunks == 2
    assert ended[0].reason == "completed"
    assert ended[0].ttfb_ms is not None
    assert ended[0].duration_ms >= 0


@pytest.mark.asyncio
async def test_stream_relay_logs_upstream_midstream_failure(caplog):
    import httpx

    with caplog.at_level(logging.INFO, logger="proxy_opencode.pipeline"):
        with pytest.raises(httpx.ReadError):
            await _collect_relay(FakeStreamRelay(exc=httpx.ReadError("mid-stream")))
    reasons = [r.reason for r in caplog.records if hasattr(r, "reason")]
    assert "relay_error:ReadError" in reasons
    ended = [r for r in caplog.records if r.getMessage() == "stream ended"]
    assert ended and ended[0].reason == "relay_error:ReadError"
    assert any(
        r.getMessage() == "stream relay failed" and r.exc_info
        for r in caplog.records
    ), "mid-stream failure must carry a traceback"


@pytest.mark.asyncio
async def test_stream_relay_logs_client_disconnect(caplog):
    from proxy_opencode.routes._pipeline import relay_stream

    gen = relay_stream(
        FakeStreamRelay().chunks(), {}, lambda status, usage=None: None,
        request_id="req1", model="m", client="127.0.0.1",
    )
    with caplog.at_level(logging.INFO, logger="proxy_opencode.pipeline"):
        await gen.__anext__()  # consume one chunk
        await gen.aclose()  # simulate client disconnect
    ended = [r for r in caplog.records if r.getMessage() == "stream ended"]
    assert len(ended) == 1
    assert ended[0].reason == "client_disconnected"
    assert ended[0].chunks == 1


@pytest.mark.asyncio
async def test_stream_relay_client_write_stall_aborts_stream(caplog):
    """A consumer that stops pulling while the generator is parked at its
    yield == the ASGI server blocked writing to the client (blackholed
    public leg). The watcher must warn, end the stream as client_stalled,
    cancel the response task and release the upstream promptly (it cannot
    rely on the generator's close, which is pinned behind cyclic GC) —
    without ever logging a completion."""
    from proxy_opencode.routes._pipeline import relay_stream

    upstream_closed = asyncio.Event()
    logged: list[int] = []

    async def upstream():
        try:
            for c in (b"a", b"b"):
                yield c
        finally:
            upstream_closed.set()

    async def drive():
        gen = relay_stream(
            upstream(), {}, lambda status, usage=None: logged.append(status),
            request_id="req1", model="m", client="127.0.0.1",
            client_stall_s=0.1,
        )
        async for _ in gen:
            # Stand in for uvicorn's send() blocking on backpressure: the
            # consumer never comes back for the next chunk.
            await asyncio.sleep(60)

    with caplog.at_level(logging.INFO, logger="proxy_opencode.pipeline"):
        task = asyncio.create_task(drive())
        for _ in range(300):
            if any(r.getMessage() == "stream ended" for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        assert upstream_closed.is_set(), "upstream must be released"
        for _ in range(300):  # cancellation delivery is scheduled, wait it out
            if task.done():
                break
            await asyncio.sleep(0.01)
        assert task.cancelled(), "response task must be cancelled on stall"

    warns = [r for r in caplog.records if r.getMessage() == "client write stalled"]
    assert len(warns) == 1
    assert warns[0].request_id == "req1"
    assert warns[0].model == "m"
    assert warns[0].client == "127.0.0.1"
    assert warns[0].chunks == 1
    assert warns[0].blocked_s >= 0.1
    ended = [r for r in caplog.records if r.getMessage() == "stream ended"]
    assert len(ended) == 1
    assert ended[0].reason == "client_stalled"
    assert ended[0].chunks == 1
    assert logged == [], "a stalled stream must not log completion"


@pytest.mark.asyncio
async def test_stream_relay_client_stall_disabled(caplog):
    """client_stall_s=0 keeps the legacy behaviour: a consumer that goes
    quiet mid-stream is not aborted and the stream can still finish."""
    from proxy_opencode.routes._pipeline import relay_stream

    logged: list[int] = []
    gen = relay_stream(
        FakeStreamRelay().chunks(), {}, lambda status, usage=None: logged.append(status),
        request_id="req1", model="m", client="127.0.0.1",
        client_stall_s=0,
    )
    with caplog.at_level(logging.INFO, logger="proxy_opencode.pipeline"):
        assert await gen.__anext__() == b"a"
        await asyncio.sleep(0.2)  # would trip an armed watchdog
        assert await gen.__anext__() == b"b"
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()  # exhausts: completion log + stream ended
    assert not [r for r in caplog.records if r.getMessage() == "client write stalled"]
    ended = [r for r in caplog.records if r.getMessage() == "stream ended"]
    assert ended[0].reason == "completed"
    assert logged == [200]


@pytest.mark.asyncio
async def test_stream_relay_slow_upstream_does_not_trip_client_watchdog(caplog):
    """The gap clock only runs while parked at the yield; a slow upstream
    (await between chunks) must never be mistaken for a stalled client."""
    from proxy_opencode.routes._pipeline import relay_stream

    async def slow_upstream():
        for c in (b"a", b"b"):
            await asyncio.sleep(0.15)  # longer than client_stall_s below
            yield c

    out: list[bytes] = []
    with caplog.at_level(logging.INFO, logger="proxy_opencode.pipeline"):
        async for chunk in relay_stream(
            slow_upstream(), {}, lambda status, usage=None: None,
            request_id="req1", model="m", client="127.0.0.1",
            client_stall_s=0.1,
        ):
            out.append(chunk)
    assert out == [b"a", b"b"]
    assert not [r for r in caplog.records if r.getMessage() == "client write stalled"]
    ended = [r for r in caplog.records if r.getMessage() == "stream ended"]
    assert len(ended) == 1
    assert ended[0].reason == "completed"
