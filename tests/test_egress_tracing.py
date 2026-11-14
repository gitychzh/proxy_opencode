"""0.8.28 regressions: ledger correlation, session identity, dead legs, route.

Found during the 12-model DSH triad pressure run (2026-11-14):
* the ledger could not be joined to a chat request (no request_id/model);
* session affinity ignored the model id, so a model sweep pinned every
  request to one egress IP and defeated the three-bucket spread;
* a probe-dead last-resort leg was still selected when all normal buckets
  were cordoned, costing a 60s TTFB stall before the backup chain answered;
* ``chat completion`` logs had no explicit primary/fallback route field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest
from starlette.requests import Request

from proxy_opencode.config import Settings
from proxy_opencode.egress import eventlog as eventlog_mod
from proxy_opencode.egress.eventlog import EVENTS, EventLog
from proxy_opencode.egress.pool import EgressPool, EgressSpec, PoolExhausted
from proxy_opencode.registry import ModelRegistry
from proxy_opencode.routes._pipeline import new_call
from proxy_opencode.upstreams.zen.client import _session_fingerprint


def _pool(*names: str, last_resort: frozenset[str] = frozenset()) -> EgressPool:
    specs = [EgressSpec(n, f"http://{n}:10001") for n in names]
    return EgressPool(specs, last_resort=last_resort)


def _healthy(pool: EgressPool) -> None:
    for state in pool._states:  # noqa: SLF001
        state.healthy = True


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )


def _call() -> Any:
    settings = Settings()
    return new_call(
        _request(),
        {"model": "deepseek-ai/deepseek-v4.1-flash", "messages": []},
        settings,
        ModelRegistry(settings),
    )


@pytest.fixture(autouse=True)
def _isolate_trace():
    """new_call() binds a ContextVar; never leak it into another test."""

    token = eventlog_mod.EVENTS._trace.set({})  # noqa: SLF001
    yield
    eventlog_mod.EVENTS._trace.reset(token)  # noqa: SLF001


# --------------------------------------------------------------- EventLog


def test_bind_fields_are_inherited_by_every_event(tmp_path):
    log = EventLog()
    log.configure(str(tmp_path / "events.jsonl"))
    log.bind(request_id="req-1", model="big-pickle")
    log.emit("egress_request", egress="mobile", ok=True)
    log.emit("cordon", egress="cc9", cordon_reason="quota")
    rows = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert all(r["request_id"] == "req-1" for r in rows)
    assert all(r["model"] == "big-pickle" for r in rows)
    assert rows[0]["egress"] == "mobile"


def test_event_specific_fields_win_over_trace(tmp_path):
    log = EventLog()
    log.configure(str(tmp_path / "events.jsonl"))
    log.bind(model="bound")
    log.emit("egress_request", model="explicit")
    assert json.loads((tmp_path / "events.jsonl").read_text())["model"] == "explicit"


@pytest.mark.asyncio
async def test_trace_is_isolated_between_concurrent_requests(tmp_path):
    log = EventLog()
    log.configure(str(tmp_path / "events.jsonl"))

    async def one(rid: str) -> None:
        log.bind(request_id=rid)
        await asyncio.sleep(0)
        log.emit("egress_request", egress=rid)

    await asyncio.gather(one("a"), one("b"))
    rows = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert {r["request_id"] for r in rows} == {"a", "b"}
    assert all(r["request_id"] == r["egress"] for r in rows)


def test_new_call_binds_ledger_correlation(tmp_path):
    token = eventlog_mod.EVENTS._trace.set({})  # noqa: SLF001
    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    try:
        call = _call()
        EVENTS.emit("egress_request", egress="mobile", ok=True)
        row = json.loads(path.read_text())
        assert row["request_id"] == call.request_id
        assert row["model"] == call.upstream_model
    finally:
        EVENTS.configure("")
        eventlog_mod.EVENTS._trace.reset(token)  # noqa: SLF001


# ------------------------------------------------------ session fingerprint


def test_fingerprint_includes_model():
    payload: dict[str, Any] = {
        "model": "big-pickle",
        "messages": [{"role": "user", "content": "same prompt"}],
    }
    same = {**payload, "stream": True}
    other = {**payload, "model": "beam-free"}
    assert _session_fingerprint(payload) == _session_fingerprint(same)
    assert _session_fingerprint(payload) != _session_fingerprint(other)
    assert _session_fingerprint({}) == ""


def test_fingerprint_still_depends_on_messages():
    first = {"model": "m", "messages": [{"role": "user", "content": "A"}]}
    second = {"model": "m", "messages": [{"role": "user", "content": "B"}]}
    assert _session_fingerprint(first) != _session_fingerprint(second)


# -------------------------------------------------------------- dead legs


@pytest.mark.asyncio
async def test_probe_dead_last_resort_never_selected():
    pool = _pool("a", "b", "slow", last_resort=frozenset({"slow"}))
    _healthy(pool)
    for state in pool._states[:2]:  # noqa: SLF001
        state.cordoned_until = time.monotonic() + 600
        state.cordon_reason = "quota"
    pool._states[2].healthy = False  # noqa: SLF001
    with pytest.raises(PoolExhausted):
        await pool.acquire()


@pytest.mark.asyncio
async def test_healthy_last_resort_serves_when_normals_cordoned():
    pool = _pool("a", "b", "slow", last_resort=frozenset({"slow"}))
    _healthy(pool)
    for state in pool._states[:2]:  # noqa: SLF001
        state.cordoned_until = time.monotonic() + 600
    lease = await pool.acquire()
    assert lease.name == "slow"
    await pool.release(lease, ok=True)


# ------------------------------------------------------------------- route


def test_chat_log_marks_primary_route(caplog):
    call = _call()
    with caplog.at_level(logging.INFO):
        call.log(logging.getLogger("proxy_opencode.chat"), 200)
    record = next(r for r in caplog.records if r.msg == "chat completion")
    assert record.route == "primary"
    assert record.request_id == call.request_id


def test_chat_log_marks_fallback_route(caplog):
    call = _call()
    call.extra_log_fields["fallback"] = "proxy_ms"
    with caplog.at_level(logging.INFO):
        call.log(logging.getLogger("proxy_opencode.chat"), 200)
    record = next(r for r in caplog.records if r.msg == "chat completion")
    assert record.route == "fallback"
