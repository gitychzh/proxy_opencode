"""Tests for the P5 rotation: counters, rotator orchestration, admin API.

Covers the frozen contract in ``egress.counters`` / ``egress.rotate``:
daily window at 08:00 Asia/Shanghai, threshold-fires-once-per-window,
hard-429 (Retry-After > 3600s or absent) bypass, ONT reboot orchestration with IP
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
from proxy_opencode.egress.eventlog import EVENTS
from proxy_opencode.egress.pool import EgressPool, EgressSpec, EgressState, Lease
from proxy_opencode.egress.rotate import EgressRotator
from proxy_opencode.routes.admin import make_router
from proxy_opencode.upstreams.zen import ZenDirectAdapter, _retry_after_seconds
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
    rotate_on_hard_429: bool = True,
    retry_cooldown_s: float = 900.0,
) -> EgressRotator:
    """Rotator wired the way a settings-driven build wires it.

    ``rotate_on_hard_429`` defaults to True *here* so the historical trigger
    tests keep testing the trigger; the shipped code default is False and
    production turns it on via ``ZEN_ROTATE_ON_HARD_429`` (the 2026-10-29
    09:15 measurement says the daily allowance is per-IP, so a fresh IP
    does buy fresh quota). The production default is pinned by
    ``test_hard_429_does_not_rotate_by_default``.
    """

    counters = EgressCounters(
        thresholds if thresholds is not None else {"a": 3},
        retry_cooldown_s=retry_cooldown_s,
    )
    return EgressRotator(
        pool,
        counters,
        poll_s=poll_s,
        timeout_s=timeout_s,
        rotate_on_hard_429=rotate_on_hard_429,
    )


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


def test_token_budget_fires_before_the_request_threshold() -> None:
    """The upstream meters tokens, not calls (2026-10-28).

    166 requests burned a whole day's allowance (~10.3M prompt tokens), so a
    request-count threshold can only ever fire after the IP is already dead.
    The token budget must trip first.
    """

    now = ts_cst(14, 9, 30)
    counters = EgressCounters({"a": 500}, token_budgets={"a": 1000})
    fired = [counters.record("a", tokens=300, now=now) for _ in range(4)]
    assert fired == [False, False, False, True]
    counts = counters.counts("a", now=now)
    assert counts.count == 4  # nowhere near the 500-request threshold
    assert counts.tokens == 1200
    assert counts.triggered is True


def test_token_budget_rollover_and_reset_clear_the_total() -> None:
    day1 = ts_cst(14, 9, 0)
    day2 = ts_cst(15, 9, 0)
    counters = EgressCounters({}, token_budgets={"a": 1000})
    assert counters.record("a", tokens=900, now=day1) is False
    # A new window starts from zero, exactly like the request counter.
    assert counters.record("a", tokens=900, now=day2) is False
    assert counters.counts("a", now=day2).tokens == 900
    # A successful rotation means a new IP, i.e. a fresh allowance.
    counters.reset("a", now=day2)
    assert counters.counts("a", now=day2).tokens == 0


def test_token_budget_survives_a_snapshot_round_trip(tmp_path) -> None:
    path = str(tmp_path / "egress_counts.json")
    now = time.time()  # inside the live window: older rows are dropped on load
    counters = EgressCounters({}, token_budgets={"a": 1000}, snapshot_path=path)
    counters.record("a", tokens=700, now=now)
    reloaded = EgressCounters({}, token_budgets={"a": 1000}, snapshot_path=path)
    assert reloaded.counts("a", now=now).tokens == 700


def test_token_only_bucket_still_rotates() -> None:
    """A bucket configured with only a token budget must be rotatable."""

    pool = make_pool("a")
    mark_healthy_with_ip(pool)
    counters = EgressCounters({}, token_budgets={"a": 1000})
    rotator = EgressRotator(pool, counters, poll_s=0.01, timeout_s=0.3)
    assert rotator.rotatable("a") is True


def test_snapshot_rows_of_retired_buckets_are_dropped_on_load(tmp_path) -> None:
    """A bucket removed from the config must not linger in the snapshot.

    2026-10-28: the retired `esp32-bucket` (dead probe target) and the `ecs`
    row survived every redeploy and kept showing up in the admin view.
    """

    path = str(tmp_path / "egress_counts.json")
    now = time.time()
    seeded = EgressCounters({"a": 3, "retired": 3}, snapshot_path=path)
    seeded.record("a", tokens=10, now=now)
    seeded.record("retired", tokens=10, now=now)
    reloaded = EgressCounters({"a": 3}, snapshot_path=path)
    assert "retired" not in reloaded.snapshot(now=now)
    assert reloaded.counts("a", now=now).count == 1


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
    assert counters.hard_quota(HARD_QUOTA_RETRY_AFTER_S) is False
    assert counters.hard_quota(HARD_QUOTA_RETRY_AFTER_S + 0.5) is True


def test_hard_quota_missing_retry_after_is_daily_exhaustion() -> None:
    """2026-10-16 measurement: quota 429s can carry no Retry-After header.

    A missing header (parsed as None) must classify as daily exhaustion,
    otherwise the hard trigger can never fire and the ONT never rotates
    on real quota cordons (that day: three cordons, all
    ``retry_after_s: null``, zero rotation requests).
    """

    counters = EgressCounters()
    assert counters.hard_quota(None) is True
    assert counters.should_rotate_on_429("a", None) is True


def test_hard_429_rotation_fires_once_per_window() -> None:
    """A headerless (hard) 429 rotates once; the flag blocks re-firing."""

    counters = EgressCounters({"a": 500})
    now = ts_cst(16, 15, 40)
    assert counters.should_rotate_on_429("a", None, now=now) is True
    counters.note_rotation_started("a", now=now)
    assert counters.counts("a", now=now).triggered is True
    assert counters.should_rotate_on_429("a", None, now=now) is False
    assert counters.should_rotate_on_429("a", 7000.0, now=now) is False
    # Successful rotation → new IP is a fresh quota bucket → flag clears.
    counters.reset("a", now=now)
    counters.note_rotation_result("a", ok=True, detail="ip 1 -> 2", now=now)
    # ...but a 429 that keeps firing right after a reboot must not reboot
    # again: only a real per-IP quota is cured by a new address, and the
    # other three 429 causes are indistinguishable (2026-10-25 storm).
    assert counters.should_rotate_on_429("a", None, now=now) is False
    assert counters.should_rotate_on_429("a", None, now=now + 1799.0) is False
    assert counters.should_rotate_on_429("a", None, now=now + 1800.0) is True


def test_hard_429_rotation_floor_can_be_disabled() -> None:
    """``hard_rotate_min_interval_s=0`` restores rotate-on-every-hard-429."""

    counters = EgressCounters({"a": 500}, hard_rotate_min_interval_s=0.0)
    now = ts_cst(16, 15, 40)
    assert counters.should_rotate_on_429("a", None, now=now) is True
    counters.note_rotation_started("a", now=now)
    counters.note_rotation_result("a", ok=True, detail="ip 1 -> 2", now=now)
    counters.reset("a", now=now)
    assert counters.should_rotate_on_429("a", None, now=now) is True


def test_free_tier_rejection_rotates_once_per_window() -> None:
    """A flagged egress IP rotates off-threshold, at most once per window."""

    counters = EgressCounters({"a": 500})
    now = ts_cst(16, 15, 40)
    assert counters.should_rotate_on_free_tier("a", now=now) is True
    counters.note_rotation_started("a", now=now)
    assert counters.should_rotate_on_free_tier("a", now=now) is False
    # Successful rotation → fresh IP → flag clears → the next rejection fires.
    counters.reset("a", now=now)
    assert counters.should_rotate_on_free_tier("a", now=now) is True


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
            self,
            name: str,
            *,
            ok: bool,
            kind: str,
            retry_after_s: float | None,
            tokens: int = 0,
        ) -> None:
            seen.append((name, ok, kind, retry_after_s))

    pool = make_pool("a")
    mark_healthy_with_ip(pool)
    pool.attach_release_observer(_Spy())
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429", retry_after_s=7200.0)
    assert seen == [("a", False, "http_429", 7200.0)]


@pytest.mark.asyncio
async def test_release_notifies_observer_on_success() -> None:
    """Regression 2026-10-17: the ok path returned before the observer ran.

    Successful (ok) releases never reached ``counters.record()``, so every
    bucket's request count stayed 0 for a whole day and the rotation
    threshold could not fire. The observer must see both outcomes.
    """

    seen: list[tuple[str, bool, str, int]] = []

    class _Spy:
        def on_release(
            self,
            name: str,
            *,
            ok: bool,
            kind: str,
            retry_after_s: float | None,
            tokens: int = 0,
        ) -> None:
            seen.append((name, ok, kind, tokens))

    pool = make_pool("a")
    mark_healthy_with_ip(pool)
    pool.attach_release_observer(_Spy())
    lease = await pool.acquire()
    # The adapter stamps the request's estimated prompt size on the lease;
    # a successful release must hand it to the counters (token budget).
    lease.tokens = 6200
    await pool.release(lease, ok=True)
    assert seen == [("a", True, "", 6200)]


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


@pytest.mark.asyncio
async def test_rotation_applied_event_carries_rotator_source_ip(
    tmp_path # type: ignore[no-untyped-def]
) -> None:
    """``rotation_applied`` must carry the rotator's captured source IP.

    The probe loop usually lands the new IP before the rotator's poll
    returns (production 2026-10-30 02:52:50 probe vs 02:53:09 poll), so
    ``state.egress_ip`` at apply time already IS the new address; falling
    back to it made the ledger show ``ip_from == ip_to`` — a phantom
    "same-IP rotation" that cost a real audit investigation.
    """

    ledger = tmp_path / "events.jsonl"
    EVENTS.configure(str(ledger))
    try:
        pool = make_pool("a")
        mark_healthy_with_ip(pool)
        lease = await pool.acquire()
        await pool.release(lease, ok=False, kind="http_429")
        state = pool.find("a")
        assert state is not None
        # The probe loop lands the fresh IP first (mid-reboot recovery):
        state.egress_ip = NEW_IP
        assert pool.apply_rotation_success("a", NEW_IP, "t1", ip_from=OLD_IP)
        rows = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        applied = [r for r in rows if r["event"] == "rotation_applied"]
        assert applied == [
            {"ts": applied[0]["ts"], "event": "rotation_applied",
             "egress": "a", "ip_from": OLD_IP, "ip_to": NEW_IP}
        ]
        assert state.cordoned_until == 0.0
    finally:
        EVENTS.configure("")


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
async def test_hard_429_without_retry_after_rotates_once() -> None:
    """2026-10-16 field day: quota 429s come with no Retry-After header.

    The headerless shape must still fire the bypass rotation, but the
    per-window flag from :meth:`note_rotation_started` must stop any
    stack-up of ONT reboots while the same window keeps 429-ing.
    """

    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 1_000_000}, poll_s=0.02, timeout_s=0.15)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )

    def _same_ip(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ready": True, "egress_ip": OLD_IP})

    respx.get("http://a:10001/healthz").side_effect = _same_ip
    rotator.on_release("a", ok=False, kind="http_429", retry_after_s=None)
    task = rotator._tasks.get("a")  # noqa: SLF001
    assert task is not None, "headerless 429 must start the bypass rotation"
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is False
    assert "unchanged" in outcome.detail
    saw = counters.counts("a")
    assert saw.rotations == 1
    assert saw.triggered is True
    # Same window, IP still stale-expired: further headerless 429s must
    # not re-trigger rotations (one per window while the flag holds).
    rotator.on_release("a", ok=False, kind="http_429", retry_after_s=None)
    assert not rotator.running("a")
    assert counters.counts("a").rotations == 1


@respx.mock
@pytest.mark.asyncio
async def test_free_tier_rejection_rotates_off_threshold_once() -> None:
    """2026-10-17: the gate flagged the egress IP → rotate it, bypassing threshold.

    The first rejection must start a rotation immediately (the bucket's
    request counter is nowhere near its threshold) and a second one in the
    same window must not stack another ONT reboot.
    """

    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 500}, poll_s=0.02, timeout_s=0.15)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )

    def _same_ip(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ready": True, "egress_ip": OLD_IP})

    respx.get("http://a:10001/healthz").side_effect = _same_ip
    rotator.on_release("a", ok=False, kind="free_tier_rejected", retry_after_s=None)
    task = rotator._tasks.get("a")  # noqa: SLF001
    assert task is not None, "a flagged IP must start a rotation bypassing threshold"
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.trigger == "free_tier_rejected"
    saw = counters.counts("a")
    assert saw.rotations == 1
    assert saw.triggered is True
    # Same window: a second rejection must not stack another reboot.
    rotator.on_release("a", ok=False, kind="free_tier_rejected", retry_after_s=None)
    assert not rotator.running("a")
    assert counters.counts("a").rotations == 1


def test_hard_429_does_not_retry_during_cooldown() -> None:
    """A failed attempt retries on the next hard 429 only after cooldown.

    Measured 2026-10-17: all three buckets failed their rotations (missing
    ONT config / prefix mismatch) and the one-per-window flag kept them
    un-rotated for the rest of the day. A cooldown gates the retry.
    """

    counters = EgressCounters({"a": 500}, retry_cooldown_s=60.0)
    t0 = 1_000_000.0
    assert counters.should_rotate_on_429("a", None, now=t0)
    counters.note_rotation_started("a", now=t0)
    counters.note_rotation_result("a", ok=False, detail="no onts", now=t0)
    # Immediately after the failure: still within the cooldown.
    assert not counters.should_rotate_on_429("a", None, now=t0 + 30.0)
    # After the cooldown has elapsed: retry allowed.
    assert counters.should_rotate_on_429("a", None, now=t0 + 61.0)


def test_hard_429_retry_blocked_while_attempt_outcome_pending() -> None:
    counters = EgressCounters({"a": 500}, retry_cooldown_s=60.0)
    t0 = 1_000_000.0
    counters.note_rotation_started("a", now=t0)
    # Started but no result recorded yet: never re-trigger concurrently
    # (the flag latches until the outcome lands).
    assert not counters.should_rotate_on_429("a", None, now=t0 + 999.0)


def test_failed_rotation_retry_not_extended_by_soft_429() -> None:
    """A soft throttle during the cooldown must not reset/extend anything."""

    counters = EgressCounters({"a": 500}, retry_cooldown_s=60.0)
    t0 = 1_000_000.0
    counters.note_rotation_started("a", now=t0)
    counters.note_rotation_result("a", ok=False, detail="x", now=t0)
    # Soft 429 (short Retry-After) never rotates, before or after cooldown.
    assert not counters.should_rotate_on_429("a", 600.0, now=t0 + 999.0)
    assert not counters.should_rotate_on_429("a", 600.0, now=t0 + 10.0)


def test_counter_snapshot_roundtrips_rotation_epoch() -> None:
    """The retry cooldown survives a restart (last_rotation_epoch persisted)."""

    import json as _json
    import os as _os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = _os.path.join(tmp, "counts.json")
        counters = EgressCounters({"a": 500}, retry_cooldown_s=60.0, snapshot_path=path)
        t0 = time.time()
        counters.note_rotation_started("a", now=t0)
        counters.note_rotation_result("a", ok=False, detail="x", now=t0)
        raw = _json.load(open(path, encoding="utf-8"))["buckets"]["a"]
        assert raw["last_rotation_epoch"] == t0
        reloaded = EgressCounters({"a": 500}, retry_cooldown_s=60.0, snapshot_path=path)
        assert reloaded.should_rotate_on_429("a", None, now=t0 + 61.0)
        assert not reloaded.should_rotate_on_429("a", None, now=t0 + 30.0)


def test_legacy_snapshot_without_epoch_recovers_cooldown() -> None:
    """Pre-epoch snapshots: parse last_rotation_at back into an epoch.

    2026-10-17 live case — the deploy restart loaded a snapshot that only
    had the formatted timestamp, and the failed-rotation retry stayed
    blocked for the rest of the window despite the cooldown long elapsed.
    """

    import json as _json
    import os as _os
    import tempfile

    from proxy_opencode.egress.pool import window_tz

    with tempfile.TemporaryDirectory() as tmp:
        path = _os.path.join(tmp, "counts.json")
        t0 = time.time()
        legacy_at = datetime.fromtimestamp(t0 - 1200.0, tz=window_tz).isoformat(
            timespec="seconds"
        )
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(
                {
                    "version": 1,
                    "buckets": {
                        "a": {
                            "window_start": 0.0,
                            "count": 0,
                            "triggered": True,
                            "rotations": 1,
                            "last_rotation_ok": False,
                            "last_rotation_at": legacy_at,
                            "last_rotation_detail": "agent rotate refused: x",
                        }
                    },
                },
                fh,
            )
        reloaded = EgressCounters({"a": 500}, retry_cooldown_s=60.0, snapshot_path=path)
        # 1200s > 60s cooldown since the parsed failure time.
        assert reloaded.should_rotate_on_429("a", None, now=t0)


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
async def test_unreachable_agent_still_polls_for_ip_change() -> None:
    """A transport-dead rotate call must not end the rotation as failed.

    The POST may already have reached the agent (or the ONT is re-dialing
    on its own). Observed 2026-10-24: opc2-eth's hard-429 rotation was
    written off as ``agent unreachable`` after its 30s call timeout while
    the tunnel flapped — and the bucket's IP verified as changed minutes
    later. When the poll later observes a fresh in-prefix IP, the rotation
    succeeds from the pool's standpoint (the ONT really did reboot).
    """

    pool = make_pool("a")
    # Cold-start probes cost hundreds of ms each (fresh httpx client per
    # poll); give the success path room despite that.
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=3.0)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").mock(
        side_effect=httpx.ConnectError("tunnel flap")
    )
    respx.get("http://a:10001/healthz").respond(
        json={"ready": True, "egress_ip": NEW_IP}
    )
    task = rotator.request_rotation("a", trigger="hard_429")
    assert task is not None
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is True
    assert outcome.ip_from == OLD_IP
    assert outcome.ip_to == NEW_IP
    assert "unreachable" in outcome.detail
    saw = counters.counts("a")
    assert saw.last_rotation_ok is True
    state = pool.find("a")
    assert state is not None
    assert state.egress_ip == NEW_IP
    assert state.rotating is False


@respx.mock
@pytest.mark.asyncio
async def test_unreachable_agent_with_stale_ip_fails_after_poll() -> None:
    """Unreachable POST *and* no IP change anywhere: still a failure, but
    the detail must carry both the transport error and the poll verdict so
    the operator can tell the two apart."""

    pool = make_pool("a")
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=0.06)
    counters = rotator.counters
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").mock(
        side_effect=httpx.ConnectTimeout("no route")
    )
    respx.get("http://a:10001/healthz").respond(
        json={"ready": True, "egress_ip": OLD_IP}
    )
    task = rotator.request_rotation("a", trigger="hard_429")
    assert task is not None
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is False
    assert "unreachable" in outcome.detail
    assert "unchanged" in outcome.detail
    saw = counters.counts("a")
    assert saw.last_rotation_ok is False


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
async def test_prefix_violation_accepted_and_flagged(caplog) -> None:  # type: ignore[no-untyped-def]
    """A changed IP outside ``expect_prefix`` is applied, with a warning.

    The prefix is a hand-maintained allowlist, so enforcing it as a rotation
    precondition deadlocked real buckets (2026-10-25: owin10-telecom's
    legitimate 117.95/49.x addresses were not listed; 11 of 21 rotations
    were recorded as failures while the bucket served fine). The new IP is
    real and the ONT did re-dial — that is a successful rotation; the stale
    allowlist is reported instead.
    """
    pool = make_pool("a")
    # Cold-start probes cost hundreds of ms; keep the deadline generous.
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=3.0)
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    respx.get("http://a:10001/healthz").respond(
        json={"ready": True, "egress_ip": "36.149.54.30"}
    )
    with caplog.at_level("WARNING", logger="proxy_opencode.egress.rotate"):
        task = rotator.request_rotation("a", trigger="admin")
        assert task is not None
        outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.ok is True
    assert outcome.ip_to == "36.149.54.30"
    assert "outside the configured prefix" in caplog.text
    state = pool.find("a")
    assert state is not None
    assert state.egress_ip == "36.149.54.30"


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
    # Generous poll deadline: each probe opens a fresh httpx client and the
    # first one costs hundreds of ms on a cold Windows test process; a tight
    # deadline can't cover the two polls this success path needs.
    rotator = make_rotator(pool, {"a": 3}, poll_s=0.01, timeout_s=3.0)
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    # First poll sees the old IP (ISP may re-issue it), every later poll
    # the fresh one — a counter instead of a two-slot queue, so extra
    # polls on a busy CI loop cannot exhaust the mock.
    polls = {"n": 0}

    def _healthz(_request: httpx.Request) -> httpx.Response:
        polls["n"] += 1
        ip = OLD_IP if polls["n"] == 1 else NEW_IP
        return httpx.Response(200, json={"ready": True, "egress_ip": ip})

    respx.get("http://a:10001/healthz").side_effect = _healthz
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

        @property
        def size(self) -> int:
            return 1

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


def test_free_tier_trigger_respects_the_rotation_floor() -> None:
    """A 403 storm must not reboot the ONT every few minutes either.

    The shape gate can re-tighten upstream, and a body/shape fault is never
    cured by a new address — so the same 30-minute floor that guards the
    hard-429 path applies to the free-tier verdict.
    """

    counters = EgressCounters({"a": 500})
    now = ts_cst(16, 15, 40)
    assert counters.should_rotate_on_free_tier("a", now=now) is True
    counters.note_rotation_started("a", now=now)
    counters.reset("a", now=now)
    counters.note_rotation_result("a", ok=True, detail="ip 1 -> 2", now=now)
    assert counters.should_rotate_on_free_tier("a", now=now) is False
    assert counters.should_rotate_on_free_tier("a", now=now + 1799.0) is False
    assert counters.should_rotate_on_free_tier("a", now=now + 1800.0) is True


@pytest.mark.asyncio
async def test_hard_429_does_not_rotate_by_default() -> None:
    """The shipped *code* default is off; production opts in via env.

    Evidence history: 0.7.12–0.7.16 measured a fresh IP rejected identically
    (account-scoped), but the 2026-10-29 09:15 measurement overturned that —
    only owin10-mobile hard-429'd while sibling buckets on the same account
    kept answering 200 (per-IP). Production therefore sets
    ZEN_ROTATE_ON_HARD_429=true; the code default stays conservative so a
    deployment without reachable agents never reboots anything.
    """

    pool = make_pool("a")
    rotator = make_rotator(
        pool, {"a": 1_000_000}, poll_s=0.02, timeout_s=0.15, rotate_on_hard_429=False
    )
    mark_healthy_with_ip(pool)

    rotator.on_release("a", ok=False, kind="http_429", retry_after_s=27000.0)

    # No rotation task was queued and the bucket's rotation count stayed 0.
    assert not rotator._tasks  # noqa: SLF001 - test seam
    assert rotator.counters.snapshot()["a"]["rotations"] == 0


def test_failed_rotation_re_arms_the_threshold_after_retry_cooldown() -> None:
    """2026-10-29 cc9: one failed rotation latched the bucket for the day.

    The ISP re-issued the same IP, the attempt was recorded as failed, and
    the ``triggered`` flag then stayed set — the burned bucket never rotated
    again and a hard 429 finally cordoned it until the 08:00 rollover. The
    threshold must re-arm once the retry cooldown has passed (a live bucket
    keeps folding requests into ``record``; the timer retry covers the
    cordoned ones).
    """

    counters = EgressCounters({}, token_budgets={"a": 100}, retry_cooldown_s=100.0)
    t0 = ts_cst(29, 10, 0)
    assert counters.record("a", tokens=100, now=t0) is True
    counters.note_rotation_started("a", now=t0)
    counters.note_rotation_result("a", ok=False, detail="ip unchanged", now=t0)
    # Inside the retry cooldown the latch still holds.
    assert counters.record("a", tokens=50, now=ts_cst(29, 10, 1)) is False
    # After the cooldown the accumulated tokens re-fire the trigger.
    assert counters.record("a", tokens=50, now=ts_cst(29, 10, 3)) is True
    # A successful rotation resets the counter: fresh quota, no fire.
    counters.note_rotation_started("a", now=ts_cst(29, 10, 3))
    counters.note_rotation_result("a", ok=True, now=ts_cst(29, 10, 4))
    counters.reset("a", now=ts_cst(29, 10, 4))
    assert counters.record("a", tokens=10, now=ts_cst(29, 10, 5)) is False


@respx.mock
@pytest.mark.asyncio
async def test_failed_rotation_is_retried_without_traffic() -> None:
    """A cordoned bucket gets no release events — the retry must be timer-driven.

    2026-10-29 cc9 live run: the first redial re-issued the same IP (failure),
    an immediate second attempt succeeded (112.83.208.198). This test replays
    exactly that shape: attempt #1 fails on the unchanged IP, the scheduled
    timer retry then rotates for real — with zero requests in between.
    """

    pool = make_pool("a")
    counters = EgressCounters({"a": 1_000_000}, retry_cooldown_s=0.05)
    rotator = EgressRotator(pool, counters, poll_s=0.01, timeout_s=0.1)
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    polls = {"new": False}

    def _healthz(_request: httpx.Request) -> httpx.Response:
        # Attempt #1's whole poll window sees the old IP; the test flips the
        # flag once that attempt has deterministically failed.
        ip = NEW_IP if polls["new"] else OLD_IP
        return httpx.Response(200, json={"ready": True, "egress_ip": ip})

    respx.get("http://a:10001/healthz").side_effect = _healthz

    first = rotator.request_rotation("a", trigger="threshold")
    assert first is not None
    outcome1 = await asyncio.wait_for(first, timeout=5.0)
    assert outcome1.ok is False
    assert "unchanged" in outcome1.detail
    polls["new"] = True

    retry = rotator._retry_tasks.get("a")  # noqa: SLF001 - test seam
    assert retry is not None, "a failed attempt must schedule a timer retry"
    await asyncio.wait_for(retry, timeout=5.0)

    second = rotator._tasks.get("a")  # noqa: SLF001 - test seam
    assert second is not None, "the timer retry must start a real rotation"
    outcome2 = await asyncio.wait_for(second, timeout=5.0)
    assert outcome2.ok is True
    assert outcome2.ip_to == NEW_IP
    state = pool.find("a")
    assert state is not None
    assert state.egress_ip == NEW_IP
    assert state.cordoned(time.monotonic()) is False


@respx.mock
@pytest.mark.asyncio
async def test_timer_retry_respects_the_per_window_cap() -> None:
    """A permanently broken ONT must not be rebooted all day."""

    pool = make_pool("a")
    counters = EgressCounters({"a": 1_000_000}, retry_cooldown_s=0.01)
    rotator = EgressRotator(
        pool, counters, poll_s=0.01, timeout_s=0.05, max_rotations_per_window=2
    )
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    respx.get("http://a:10001/healthz").side_effect = lambda _r: httpx.Response(
        200, json={"ready": True, "egress_ip": OLD_IP}
    )

    first = rotator.request_rotation("a", trigger="threshold")
    assert first is not None
    await asyncio.wait_for(first, timeout=5.0)  # failure #1 (rotations=1)
    retry = rotator._retry_tasks.get("a")  # noqa: SLF001
    assert retry is not None
    await asyncio.wait_for(retry, timeout=5.0)
    second = rotator._tasks.get("a")  # noqa: SLF001
    assert second is not None
    await asyncio.wait_for(second, timeout=5.0)  # failure #2 (rotations=2)

    # Cap reached: the failure path must not schedule another retry.
    await asyncio.sleep(0.08)
    pending = rotator._retry_tasks.get("a")  # noqa: SLF001
    assert pending is None or pending.done()
    assert counters.counts("a").rotations == 2

@pytest.mark.asyncio
async def test_request_rotation_enforces_the_cap_on_every_trigger() -> None:
    """The per-window cap must bind the TRIGGER paths, not only retries.

    2026-10-29: the cap lived only in the failed-retry loop, so with
    ZEN_ROTATE_ON_HARD_429 a bucket whose rotations kept *succeeding*
    re-armed its own trigger after every attempt (reset() clears the
    triggered flag) and logged 6 rotations against a cap of 5 -- each fresh
    IP 429'd again within minutes, i.e. exactly the reboot storm the cap
    exists to prevent.
    """

    pool = make_pool("a")
    counters = EgressCounters({"a": 1_000_000}, retry_cooldown_s=0.01)
    rotator = EgressRotator(
        pool, counters, poll_s=0.01, timeout_s=0.05, max_rotations_per_window=2
    )
    mark_healthy_with_ip(pool)
    respx.post("http://a:10001/rotate").respond(
        json={"ok": True, "results": [{"ok": True, "ont": "x", "detail": ""}]}
    )
    # healthz always answers with a CHANGED ip so every attempt succeeds.
    ips = iter(f"117.95.231.{n}" for n in range(10, 100))
    respx.get("http://a:10001/healthz").mock(
        side_effect=lambda _r: httpx.Response(
            200, json={"ready": True, "egress_ip": next(ips)}
        )
    )

    first = rotator.request_rotation("a", trigger="hard_429")
    assert first is not None
    await asyncio.wait_for(first, timeout=5.0)  # success #1 (rotations=1)
    second = rotator.request_rotation("a", trigger="hard_429")
    assert second is not None
    await asyncio.wait_for(second, timeout=5.0)  # success #2 (rotations=2)

    # Cap reached: the trigger path must refuse now -- this is the regression.
    third = rotator.request_rotation("a", trigger="hard_429")
    assert third is None
    assert counters.counts("a").rotations == 2

    # Admin requests bypass the cap on purpose.
    forced = rotator.request_rotation("a", trigger="admin", force=True)
    assert forced is not None
    await asyncio.wait_for(forced, timeout=5.0)
    assert counters.counts("a").rotations == 3
