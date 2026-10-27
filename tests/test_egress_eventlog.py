"""Event log (JSONL) for the 0.7.x egress layer.

Covers the audit trail the owner asked for: rotation events with before/
after IP comparison, cordons, probe-observed IP changes, window rollovers —
all written to one file on the ECS host, never raising, silently disabling
itself on I/O failures.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx

from proxy_opencode.egress import eventlog as eventlog_mod
from proxy_opencode.egress.counters import EgressCounters
from proxy_opencode.egress.eventlog import EVENTS
from proxy_opencode.egress.rotate import EgressRotator
from tests.test_egress_rotation import (
    NEW_IP,
    OLD_IP,
    make_pool,
    mark_healthy_with_ip,
)


def read_events(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(autouse=True)
def _clean_event_sink():
    yield
    EVENTS.configure("")


# ------------------------------------------------------------------ EventLog


def test_disabled_by_default_writes_nothing(tmp_path):
    stray = tmp_path / "unused.jsonl"
    EVENTS.emit("egress_ip_observed", egress="a", previous="1", current="2")
    assert not stray.exists()


def test_emit_writes_jsonl_with_timestamp_and_fields(tmp_path):
    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    EVENTS.emit("rotation_succeeded", egress="owin10-telecom", ip_from=OLD_IP, ip_to=NEW_IP)
    EVENTS.emit("cordon", egress="cc9", cordon_reason="free_quota_exhausted")
    events = read_events(str(path))
    assert [e["event"] for e in events] == [
        "rotation_succeeded",
        "cordon",
    ]
    assert events[0]["ip_from"] == OLD_IP
    assert events[0]["ip_to"] == NEW_IP
    assert events[1]["cordon_reason"] == "free_quota_exhausted"
    # UTC ISO timestamps, sortable, one per line.
    assert events[0]["ts"].endswith("+00:00")
    assert events[1]["ts"] >= events[0]["ts"]


def test_write_failure_disables_sink_without_raising(tmp_path):
    # A directory cannot be opened for append: OSError -> one-time disable.
    EVENTS.configure(str(tmp_path))  # path IS a directory
    EVENTS.emit("cordon", egress="a")  # must not raise
    EVENTS.emit("cordon", egress="a")
    assert EVENTS._disabled  # noqa: SLF001 - test seam


def test_size_rotation_keeps_two_rotated_files(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    monkeypatch.setattr(eventlog_mod, "_MAX_BYTES", 150)
    for i in range(6):
        EVENTS.emit("egress_ip_observed", egress=f"bucket-{i:020}", previous="x", current="y")
    # Main file back in place (rotated), plus path.1/path.2 rotated layers.
    assert os.path.exists(str(path))
    assert os.path.exists(str(path) + ".1")
    assert os.path.exists(str(path) + ".2")
    rotated = read_events(str(path) + ".2")
    assert rotated[0]["event"] == "egress_ip_observed"
    # path.3 must never exist (bounded keep).
    assert not os.path.exists(str(path) + ".3")


# ------------------------------------------------------- rotation event wire


@pytest.mark.asyncio
async def test_rotation_flow_writes_full_event_timeline(tmp_path):
    """The requested ask: an observable timeline with IPs before/after."""
    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    pool = make_pool("a")
    mark_healthy_with_ip(pool, OLD_IP)
    counters = EgressCounters({"a": 500})
    # Two successful probes needed; the first costs a cold httpx client.
    rotator = EgressRotator(pool, counters, poll_s=0.01, timeout_s=3.0)
    with respx.mock:
        respx.post("http://a:10001/rotate").respond(
            json={"ok": True, "results": [{"ok": True, "detail": ""}]}
        )
        health = respx.get("http://a:10001/healthz")
        health.side_effect = [
            # Mid-reboot: agent reachable but no egress IP yet.
            httpx.Response(200, json={"ready": False}),
            # Recovered with the NEW ISP address.
            httpx.Response(200, json={"ready": True, "egress_ip": NEW_IP}),
        ]
        outcome = await rotator._rotate("a", trigger="threshold")  # noqa: SLF001
    assert outcome.ok
    assert outcome.ip_from == OLD_IP
    assert outcome.ip_to == NEW_IP
    events = read_events(str(path))
    names = [e["event"] for e in events]
    assert names == [
        "rotation_started",
        "rotation_applied",
        "rotation_succeeded",
    ]
    started, applied, succeeded = events
    assert started["ip_from"] == OLD_IP
    assert succeeded["ip_from"] == OLD_IP
    assert succeeded["ip_to"] == NEW_IP
    assert succeeded["trigger"] == "threshold"
    # Applied: the pool side recorded the live swap as well.
    assert applied["ip_from"] == OLD_IP
    assert applied["ip_to"] == NEW_IP


@pytest.mark.asyncio
async def test_rotation_failure_writes_failed_event(tmp_path):
    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    pool = make_pool("a")
    mark_healthy_with_ip(pool, OLD_IP)
    counters = EgressCounters({"a": 500})
    rotator = EgressRotator(pool, counters, poll_s=0.01, timeout_s=0.2)
    with respx.mock:
        respx.post("http://a:10001/rotate").respond(
            json={"ok": True, "results": [{"ok": True, "detail": ""}]}
        )
        # Agent stays "rebooted" with no IP: poll loop times out.
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False}
        )
        outcome = await rotator._rotate("a", trigger="hard_429")  # noqa: SLF001
    assert not outcome.ok
    events = read_events(str(path))
    failed = [e for e in events if e["event"] == "rotation_failed"]
    assert len(failed) == 1
    assert failed[0]["ip_from"] == OLD_IP
    assert "unchanged" in failed[0]["detail"]
