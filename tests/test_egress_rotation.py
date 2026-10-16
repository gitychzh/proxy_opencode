"""Tests for the P5 rotation: counters, rotator orchestration, admin API.

Covers the frozen contract in ``egress.counters`` / ``egress.rotate``:
daily window at 08:00 Asia/Shanghai, threshold-fires-once-per-window,
hard-429 (Retry-After > 3600s) bypass, ONT reboot orchestration with IP
verification, and the admin trigger endpoint.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi import FastAPI

from proxy_opencode.auth.keystore import KeyStore
from proxy_opencode.config import Settings, _parse_kv_int
from proxy_opencode.egress.counters import HARD_QUOTA_RETRY_AFTER_S, EgressCounters
from proxy_opencode.egress.pool import EgressPool, EgressSpec, EgressState, Lease
from proxy_opencode.egress.rotate import EgressRotator
from proxy_opencode.routes.admin import make_router
from proxy_opencode.upstreams.zen_direct import ZenDirectAdapter, _retry_after_seconds
from tests.test_zen_direct import make_settings

CST = timezone(timedelta(hours=8))

OLD_IP = "117.95.231.70"
NEW_IP = "117.95.231.99"
PREFIX_OLD_BUCKET = "117.95.231.0/24"


def ts_cst(*args: int) -> float:
    """2026-10-14 style wall-clock args as epoch seconds (Asia/Shanghai)."""

    day = int(args[0])
    hour, minute = args[1], args[2]
    return datetime(2026, 10, day, hour, minute, tzinfo=CST).timestamp()


def make_pool(*names: str, **kw: object) -> EgressPool:
    specs = [
        EgressSpec(n, f"http://{n}:10001", PREFIX_OLD_BUCKET if n == "a" else None)
        for n in names
    ]
    return EgressPool(specs, **kw)  # type: ignore[arg-type]


def mark_healthy_with_ip(pool: EgressPool, ip: str = OLD_IP) -> None:
    for state in pool._states:  # noqa: SLF001 - test seam
        state.healthy = True
        state.egress_ip = ip


def make_rotator(
    pool: EgressPool,
    thresholds: dict[str, int] | None = None,
    *,
    poll_s: float = 0.01,
    timeout_s: float = 0.3,
) -> EgressRotator:
    counters = EgressCounters(thresholds if thresholds is not None else {"a": 3})
    return EgressRotator(pool, counters, poll_s=poll_s, timeout_s=timeout_s)


# ----------------------------------------------------------------- counters


def test_window_start_math() -> None:
    counters = EgressCounters({"a": 500})
    start = counters.window_start(ts_cst(14, 9, 30))
    assert datetime.fromtimestamp(
        start, tz=CST
    ) == datetime(2026, 10, 14, 8, 0, tzinfo=CST)
    # 07:30 is before the window start -> the window opened the previous day.
    early = counters.window_start(ts_cst(14, 7, 30))
    assert datetime.fromtimestamp(early, tz=CST) == datetime(
        2026, 10, 13, 8, 0, tzinfo=CST
    )


def test_threshold_fires_exactly_once_and_resets_on_rotation() -> None:
    now = ts_cst(14, 9, 30)
    counters = EgressCounters({"a": 3})
    fired = [counters.record("a", now=now) for _ in range(5)]
    assert fired == [False, False, True, False, False]
    # A successful rotation restarts the counter: new IP = fresh bucket.
    counters.reset("a", now=now)
    counts = counters.counts("a", now=now)
    assert counts.count == 0
    assert counts.triggered is False
    assert counters.record("a", now=now) is False
    assert counters.record("a", now=now) is False
    assert counters.record("a", now=now) is True


def test_daily_rollover_at_0800_resets_every_counter() -> None:
    day1 = ts_cst(14, 9, 0)
    day2 = ts_cst(15, 9, 0)
    counters = EgressCounters({"a": 3})
    assert [counters.record("a", now=day1) for _ in range(3)] == [
        False,
        False,
        True,
    ]
    # Crossing 08:00 must not carry the previous window over.
    assert counters.record("a", now=day2) is False
    counts = counters.counts("a", now=day2)
    assert counts.count == 1
    assert counts.triggered is False
    assert counts.rotations == 0
    assert datetime.fromtimestamp(
        counts.window_start, tz=CST
    ) == datetime(2026, 10, 15, 8, 0, tzinfo=CST)


def test_hard_quota_boundary_is_strictly_greater_than_3600s() -> None:
    counters = EgressCounters()
    assert counters.hard_quota(None) is False
    assert counters.hard_quota(600.0) is False
    assert counters.hard_quota(HARD_QUOTA_RETRY_AFTER_S) is False
    assert counters.hard_quota(HARD_QUOTA_RETRY_AFTER_S + 0.5) is True


def test_snapshot_is_admin_safe() -> None:
    now = ts_cst(14, 9, 0)
    counters = EgressCounters({"a": 500})
    counters.record("a", now=now)
    snap = counters.snapshot(now=now)
    assert set(snap) == {"a"}
    entry = snap["a"]
    assert entry["count"] == 1
    assert entry["threshold"] == 500
    assert entry["triggered"] is False
    assert "secret" not in " ".join(str(k) for k in entry.keys())


def test_snapshot_persists_and_reloads(tmp_path) -> None:
    path = str(tmp_path / "egress_counts.json")
    now = ts_cst(14, 9, 0)
    counters = EgressCounters({"a": 500}, snapshot_path=path)
    counters.record("a", now=now)
    counters.record("a", now=now)
    counters.note_rotation_result("a", ok=True, detail="ip 1 -> 2", now=now)
    reloaded = EgressCounters({"a": 500}, snapshot_path=path)
    reloaded.load(now=ts_cst(14, 23, 0))  # same window: counts must survive
    assert reloaded.counts("a", now=ts_cst(14, 23, 0)).count == 2
    assert reloaded.counts("a", now=now).count == 2
    assert reloaded.counts("a", now=now).last_rotation_ok is True
    # Nothing secret in the file, ever.
    with open(path, encoding="utf-8") as fh:
        assert "secret" not in fh.read().lower()


def test_corrupt_snapshot_starts_fresh(tmp_path) -> None:
    path = tmp_path / "egress_counts.json"
    path.write_text("{ not json", encoding="utf-8")
    counters = EgressCounters({"a": 500}, snapshot_path=str(path))
    now = ts_cst(14, 9, 0)
    assert counters.counts("a", now=now).count == 0


def test_stale_windows_from_previous_day_are_dropped_on_load(tmp_path) -> None:
    path = str(tmp_path / "egress_counts.json")
    counters = EgressCounters({"a": 500}, snapshot_path=path)
    counters.record("a", now=ts_cst(14, 9, 0))
    # Loaded after the window rolled over -> the old count must not leak
    # into the new window (restart just after 08:00 CST).
    restored = EgressCounters({"a": 500}, snapshot_path=path)
    restored.load(now=ts_cst(15, 9, 0))
    counts = restored.counts("a", now=ts_cst(15, 9, 0))
    assert counts.count == 0
    assert counts.triggered is False


# --------------------------------------------------------------- retry-after


def test_retry_after_parsing() -> None:
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds({}) is None
    assert _retry_after_seconds({"retry-after": "120"}) == 120.0
    assert _retry_after_seconds({"retry-after": " 300 "}) == 300.0
    assert _retry_after_seconds({"retry-after": "garbage"}) is None
    later = email.utils.format_datetime(
        datetime.now(tz=timezone.utc) + timedelta(seconds=3600)
    )
    parsed = _retry_after_seconds({"retry-after": later})
    assert parsed is not None
    assert 3590.0 < parsed <= 3600.0


def test_settings_parse_rotation_thresholds() -> None:
    assert _parse_kv_int("a=500,b=512") == {"a": 500, "b": 512}
    with pytest.raises(ValueError):
        _parse_kv_int("a=abc")
    with pytest.raises(ValueError):
        _parse_kv_int("a=0")
    with pytest.raises(ValueError):
        _parse_kv_int("a=")


# ------------------------------------------------------------------- rotator


@respx.mock
@pytest.mark.asyncio
async def test_threshold_triggers_rotation_and_resets_counter() -> None:
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=2.0)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "hg8020c", "detail": ""}]}
    )
    respx.get("http://a:10001/healthz").side_effect = [
        httpx.Response(200, json={"ready": True, "egress_ip": OLD_IP}),
        httpx.Response(200, json={"ready": True, "egress_ip": NEW_IP}),
    ]
    third = None
    for _ in range(3):
        rotator.on_release("a", ok=True, kind="", retry_after_s=None)
        third = counters.counts("a").count
    assert third == 3
    task = rotator._tasks.get("a")  # noqa: SLF001 - trigger inspection
    assert task is not None
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is True
    assert outcome.ip_from == OLD_IP
    assert outcome.ip_to == NEW_IP
    state = pool.find("a")
    assert state is not None
    assert state.egress_ip == NEW_IP
    assert state.healthy is True
    assert state.rotating is False
    # The counter restarted for the fresh IP (count 0 + armed threshold).
    counts = counters.counts("a")
    assert counts.count == 0
    assert counts.triggered is False
    filled = [counters.record("a") for _ in range(3)]
    assert filled == [False, False, True]  # threshold re-arms after reset


@pytest.mark.asyncio
async def test_acquire_skips_rotating_bucket() -> None:
    pool = make_pool("a", "b")
    mark_healthy_with_ip(pool)
    assert pool.mark_rotating("a", True) is True
    lease = await pool.acquire()
    # "a" is mid-ONT-reboot: only "b" may serve.
    assert lease.name == "b"
    # Last resort also refuses rotating egresses (dead during reboot).
    lease_b = await pool.acquire()
    await pool.release(lease_b, ok=True)
    other = await pool.acquire()
    assert other.name == "b"
    await pool.release(other, ok=True)
    pool.mark_rotating("a", False)
    assert await pool.acquire() is not None


@pytest.mark.asyncio
async def test_release_notifies_observer_with_retry_after() -> None:
    seen: list[tuple[str, bool, str, float | None]] = []

    class _Spy:
        def on_release(
            self, name: str, *, ok: bool, kind: str, retry_after_s: float | None
        ) -> None:
            seen.append((name, ok, kind, retry_after_s))

    pool = make_pool("a")
    mark_healthy_with_ip(pool)
    pool.attach_release_observer(_Spy())
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429", retry_after_s=7200.0)
    assert seen == [("a", False, "http_429", 7200.0)]


@pytest.mark.asyncio
async def test_quota_cordon_cleared_by_rotation_success() -> None:
    pool = make_pool("a")
    mark_healthy_with_ip(pool)
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429")
    state = pool.find("a")
    assert state is not None
    assert state.cordoned(time.monotonic())
    pool.apply_rotation_success("a", NEW_IP, "t1")
    assert state.cordoned_until == 0.0
    assert state.healthy is True
    assert state.egress_ip == NEW_IP


@respx.mock
@pytest.mark.asyncio
async def test_hard_429_bypasses_threshold_and_rotates() -> None:
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 1_000_000}, poll_s=0.02, timeout_s=0.15)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    # The ISP keeps re-issuing the same address -> the rotation fails
    # loudly, but the hard-429 trigger itself must have fired at once.
    def _same_ip(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ready": True, "egress_ip": OLD_IP})

    respx.get("http://a:10001/healthz").side_effect = _same_ip
    rotator.on_release("a", ok=False, kind="http_429", retry_after_s=7200.0)
    task = rotator._tasks.get("a")  # noqa: SLF001
    assert task is not None, "hard 429 must start a rotation bypassing threshold"
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is False
    assert "unchanged" in outcome.detail
    saw = counters.counts("a")
    assert saw.last_rotation_ok is False
    assert saw.rotations == 1
    # A soft throttle (< 1h) must NOT trigger a rotation.
    rotator.on_release("a", ok=False, kind="http_429", retry_after_s=600.0)
    assert not rotator.running("a")


@respx.mock
@pytest.mark.asyncio
async def test_agent_failure_reported_and_no_state_change() -> None:
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=0.2)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(status_code=500, json={"ok": False})
    task = rotator.request_rotation("a", trigger="admin")
    assert task is not None
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is False
    assert "status 500" in outcome.detail
    saw = counters.counts("a")
    assert saw.last_rotation_ok is False
    assert saw.rotations == 1
    state = pool.find("a")
    assert state is not None
    assert state.healthy is True  # rotation failure ≠ egress down
    assert state.rotating is False


@respx.mock
@pytest.mark.asyncio
async def test_unchanged_ip_times_out_as_failed_rotation() -> None:
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.02, timeout_s=0.1)
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    respx.get("http://a:10001/healthz").respond(
        json={"ready": True, "egress_ip": OLD_IP}
    )
    task = rotator.request_rotation("a", trigger="admin")
    assert task is not None
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is False
    assert "unchanged" in outcome.detail


@respx.mock
@pytest.mark.asyncio
async def test_prefix_violation_refused() -> None:
    """A reboot that lands the leg on another uplink must NOT be applied."""
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=0.5)
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    respx.get("http://a:10001/healthz").respond(
        json={"ready": True, "egress_ip": "36.149.54.30"}
    )
    task = rotator.request_rotation("a", trigger="admin")
    assert task is not None
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is False
    assert "outside bucket prefix" in outcome.detail
    state = pool.find("a")
    assert state is not None
    assert state.egress_ip == OLD_IP  # the foreign IP is not folded in


@respx.mock
@pytest.mark.asyncio
async def test_rotatable_and_running_guards() -> None:
    pool = make_pool("a", "pad")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=0.2)
    mark_healthy_with_ip(pool)
    # Non-rotatable buckets (no ONT config, e.g. the ECS host agent).
    assert rotator.request_rotation("pad", trigger="admin") is None
    # At most one rotation per bucket at a time.
    release_rotate = asyncio.Event()

    async def _slow_rotate(_request: httpx.Request) -> httpx.Response:
        await release_rotate.wait()
        return httpx.Response(
            200, json={"ok": True, "results": [{"ok": True, "detail": ""}]}
        )

    respx.post("http://a:10001/rotate").side_effect = _slow_rotate
    respx.get("http://a:10001/healthz").respond(
        json={"ready": True, "egress_ip": OLD_IP}
    )
    task = rotator.request_rotation("a", trigger="admin")
    assert task is not None
    assert rotator.request_rotation("a", trigger="admin") is None
    assert rotator.request_rotation("pad", trigger="admin") is None
    release_rotate.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert not rotator.running("a")


@respx.mock
@pytest.mark.asyncio
async def test_aclose_cancels_inflight_rotation() -> None:
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=30.0)
    mark_healthy_with_ip(pool)
    release_rotate = asyncio.Event()

    async def _slow_rotate(_request: httpx.Request) -> httpx.Response:
        await release_rotate.wait()
        return httpx.Response(200, json={"ok": True, "results": [{"ok": True}]})

    respx.post("http://a:10001/rotate").side_effect = _slow_rotate
    task = rotator.request_rotation("a", trigger="admin")
    assert task is not None
    await rotator.aclose()
    assert rotator.counters.counts("a").last_rotation_ok is False
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)


# ------------------------------------------------------------------- admin API


ADMIN_HEADERS = {"Authorization": "Bearer dev-admin-key"}


def _admin_app(
    tmp_path,
    pool: EgressPool,
    rotator: EgressRotator | None,
) -> FastAPI:
    app = FastAPI()
    from proxy_opencode.errors import GatewayHttpError

    @app.exception_handler(GatewayHttpError)
    async def _gateway_error(request, exc):  # type: ignore[no-untyped-def]
        return exc.response

    settings = Settings(
        gateway_api_keys=["gw-key"],
        admin_api_keys=["dev-admin-key"],
    )
    keystore = KeyStore(str(tmp_path / "keys.json"))
    app.include_router(make_router(settings, keystore, pool, rotator))
    return app


@respx.mock
@pytest.mark.asyncio
async def test_admin_egresses_snapshot_includes_counters(tmp_path) -> None:
    pool = make_pool("a")
    counters = EgressCounters({"a": 3})
    rotator = EgressRotator(pool, counters, poll_s=0.01, timeout_s=0.2)
    counters.record("a")
    app = _admin_app(tmp_path, pool, rotator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/admin/egresses", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
    assert body["pool"] is True
    assert body["egresses"][0]["name"] == "a"
    assert body["counters"]["a"]["threshold"] == 3
    assert "secret" not in json.dumps(body).lower()


@respx.mock
@pytest.mark.asyncio
async def test_admin_rotate_endpoints(tmp_path) -> None:
    pool = make_pool("a", "pad")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=0.3)
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    respx.get("http://a:10001/healthz").side_effect = [
        httpx.Response(200, json={"ready": True, "egress_ip": OLD_IP}),
        httpx.Response(200, json={"ready": True, "egress_ip": NEW_IP}),
    ]
    app = _admin_app(tmp_path, pool, rotator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.post("/admin/egresses/pad/rotate", headers=ADMIN_HEADERS)
        assert resp.status_code == 400
        resp = await client.post(
            "/admin/egresses/unknown/rotate", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404
        resp = await client.post("/admin/egresses/a/rotate", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"started": True, "name": "a"}
        # Wait for the spawned rotation to drain the test properly.
        task = rotator._tasks.get("a")  # noqa: SLF001 - test seam
        assert task is not None
        await asyncio.wait_for(task, timeout=5.0)
        resp = await client.get("/admin/egresses", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        entry = resp.json()["counters"]["a"]
        assert entry["last_rotation_ok"] is True
        state = pool.find("a")
        assert state is not None
        assert state.egress_ip == NEW_IP
    assert rotator.counters.counts("a").count == 0  # reset for the fresh IP


@pytest.mark.asyncio
async def test_admin_rotate_requires_admin_key(tmp_path) -> None:
    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3})
    app = _admin_app(tmp_path, pool, rotator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.post("/admin/egresses/a/rotate")
        assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_legacy_egresses_payload_without_rotator(tmp_path) -> None:
    pool = make_pool("a")
    app = _admin_app(tmp_path, pool, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/admin/egresses", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
    assert body == {"pool": True, "egresses": pool.snapshot(), "counters": {}}


# ------------------------------------------------------------ zen-direct wiring


@respx.mock
@pytest.mark.asyncio
async def test_zen_429_release_carries_retry_after() -> None:
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=429,
        headers={"retry-after": "7200"},
        json={"error": {"message": "quota"}},
    )

    class _StubPool:
        def __init__(self) -> None:
            self.releases: list[tuple[str, bool, str, float | None]] = []

        async def acquire(self) -> Lease:
            state = EgressState(spec=EgressSpec("bucket-a", "http://agent:10001"))
            state.client = httpx.AsyncClient()
            state.inflight += 1
            return Lease(state)

        async def release(
            self,
            lease: Lease,
            *,
            ok: bool,
            kind: str = "",
            retry_after_s: float | None = None,
        ) -> None:
            self.releases.append((lease.name, ok, kind, retry_after_s))
            await lease.state.client.aclose()

    pool = _StubPool()
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": "q"}],
        }
    )
    assert result["__status__"] == 429
    assert pool.releases == [("bucket-a", False, "http_429", 7200.0)]
    await adapter.aclose()
