"""Tests for the 0.7.x egress pool (selection, cordon, probes, config)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from proxy_opencode.config import Settings
from proxy_opencode.egress.pool import (
    EgressPool,
    EgressSpec,
    EgressState,
    next_window_reset,
    seconds_to_window_reset,
)

_TZ = timezone(timedelta(hours=8))


def make_pool(*names: str, **kw: object) -> EgressPool:
    specs = [EgressSpec(n, f"http://{n}:10001") for n in names]
    return EgressPool(specs, **kw)  # type: ignore[arg-type]


def mark_healthy(pool: EgressPool) -> None:
    for state in pool._states:  # noqa: SLF001 - test seam for deterministic setup
        state.healthy = True


# ------------------------------------------------------------------- specs


def test_spec_validation() -> None:
    with pytest.raises(ValueError):
        EgressSpec("", "http://a:10001")
    with pytest.raises(ValueError):
        EgressSpec("a/b", "http://a:10001")
    with pytest.raises(ValueError):
        EgressSpec("a", "ftp://a:10001")
    with pytest.raises(ValueError):
        EgressSpec("a", "http://a:10001", expect_prefix="not-a-network")


def test_matches_prefix() -> None:
    spec = EgressSpec("a", "http://a:10001", expect_prefix="117.95.231.0/24")
    assert spec.matches_prefix("117.95.231.70")
    assert not spec.matches_prefix("36.149.54.30")
    assert not spec.matches_prefix("garbage")
    # no prefix configured -> never restricted
    assert EgressSpec("a", "http://a:10001").matches_prefix("1.2.3.4")


def test_matches_prefix_accepts_comma_separated_cidrs() -> None:
    """ISPs hand out several /24 blocks per region (mobile, 2026-10-17)."""

    spec = EgressSpec(
        "a", "http://a:10001", expect_prefix="36.149.54.0/24; 223.68.27.0/24"
    )
    assert spec.matches_prefix("36.149.54.30")
    assert spec.matches_prefix("223.68.27.189")
    assert not spec.matches_prefix("180.117.204.212")
    assert not spec.matches_prefix("112.83.208.127")
    # env-var shapes use semicolons (kv pairs own the comma); memory
    # shapes may use commas; both split identically.
    spec2 = EgressSpec(
        "a", "http://a:10001", expect_prefix="36.149.54.0/24,223.68.27.0/24"
    )
    assert spec2.matches_prefix("223.68.27.189")
    # malformed single part still rejected at construction time
    with pytest.raises(ValueError):
        EgressSpec("a", "http://a:10001", expect_prefix="1.2.3.0/24; not-a-network")
    # unknown IP with a prefix configured must not pass
    assert not spec.matches_prefix(None)


def test_next_window_reset_is_next_0800_shanghai() -> None:
    now = datetime(2026, 10, 14, 9, 30, tzinfo=_TZ)
    reset = datetime.fromtimestamp(next_window_reset(now), _TZ)
    assert reset == datetime(2026, 10, 15, 8, 0, tzinfo=_TZ)
    # before 08:00 -> same day
    early = datetime(2026, 10, 14, 3, 0, tzinfo=_TZ)
    reset2 = datetime.fromtimestamp(next_window_reset(early), _TZ)
    assert reset2 == datetime(2026, 10, 14, 8, 0, tzinfo=_TZ)


def test_from_settings_none_when_unset() -> None:
    assert EgressPool.from_settings(Settings()) is None


def test_from_settings_builds_specs_and_prefixes() -> None:
    settings = Settings(
        zen_egress_proxies={
            "owin10-mobile": "http://100.0.0.1:10001",
            "owin10-telecom": "http://100.0.0.1:10002",
        },
        zen_egress_expect_prefix={"owin10-mobile": "117.95.231.0/24"},
        zen_egress_secret="s3cret",
    )
    pool = EgressPool.from_settings(settings)
    assert pool is not None
    assert pool.names() == ["owin10-mobile", "owin10-telecom"]
    wifi = pool._states[0].spec  # noqa: SLF001
    assert wifi.agent_url == "http://100.0.0.1:10001"
    assert wifi.expect_prefix == "117.95.231.0/24"
    assert pool._states[1].spec.expect_prefix is None  # noqa: SLF001


def test_empty_pool_rejected() -> None:
    with pytest.raises(ValueError):
        EgressPool([])


# --------------------------------------------------------------- selection


@pytest.mark.asyncio
async def test_acquire_picks_least_inflight_with_round_robin_ties() -> None:
    pool = make_pool("a", "b")
    mark_healthy(pool)
    l1 = await pool.acquire()
    assert l1.name == "a"  # all idle -> rotation starts at the first bucket
    l2 = await pool.acquire()
    assert l2.name == "b"  # a now has one in flight
    await pool.release(l1, ok=True)
    l3 = await pool.acquire()
    assert l3.name == "a"


@pytest.mark.asyncio
async def test_acquire_round_robins_idle_buckets() -> None:
    """Sequential traffic must spread across buckets, not pin to one.

    Least-in-flight alone ties when every bucket is idle; the old
    name-based tie-break always returned the alphabetically-first bucket
    and drained its per-IP daily quota first (found in the 2026-10-15 ECS
    e2e, where 40 sequential requests all hit ``cc9``).
    """

    pool = make_pool("a", "b", "c")
    mark_healthy(pool)
    seen: list[str] = []
    for _ in range(6):
        lease = await pool.acquire()
        seen.append(lease.name)
        await pool.release(lease, ok=True)  # idle again -> pure rotation
    assert seen == ["a", "b", "c", "a", "b", "c"]


@pytest.mark.asyncio
async def test_acquire_skips_unhealthy_but_uses_cordoned_as_last_resort() -> None:
    pool = make_pool("a", "b")
    pool._states[0].healthy = True  # noqa: SLF001
    pool._states[1].healthy = False  # noqa: SLF001
    lease = await pool.acquire()
    assert lease.name == "a"
    # both unhealthy/cordoned: still hand out a lease rather than inventing a 502
    pool._states[0].healthy = False  # noqa: SLF001
    lease2 = await pool.acquire()
    assert lease2.name in {"a", "b"}


@pytest.mark.asyncio
async def test_last_resort_bucket_only_absorbs_overflow() -> None:
    """A weak uplink must not take an equal share of the round robin.

    Measured 2026-10-29: the ESP32-C3 bucket needs ~15s to push a 20KB prompt
    body, so its first byte misses ZEN_TTFB_S=60 on a busy turn and the client
    waits the full minute before the gateway fails over. With it in the normal
    rotation every fourth turn paid that minute, which is exactly the "很卡"
    the operator reported after the 4th bucket joined.
    """

    pool = make_pool("fast1", "fast2", "slow", last_resort=frozenset({"slow"}))
    mark_healthy(pool)
    seen = []
    for _ in range(8):
        lease = await pool.acquire()
        seen.append(lease.name)
        await pool.release(lease, ok=True)
    assert "slow" not in seen
    assert set(seen) == {"fast1", "fast2"}

    # Every normal bucket gone -> the last-resort one serves instead of 502.
    for state in pool._states[:2]:
        state.healthy = False
    lease = await pool.acquire()
    assert lease.name == "slow"


@pytest.mark.asyncio
async def test_last_resort_unknown_name_is_ignored() -> None:
    """A retired bucket name must not make the whole pool last-resort."""

    pool = make_pool("a", "b", last_resort=frozenset({"retired"}))
    mark_healthy(pool)
    seen = {(await pool.acquire()).name for _ in range(2)}
    assert seen == {"a", "b"}


@pytest.mark.asyncio
async def test_release_ok_resets_failures() -> None:
    pool = make_pool("a")
    state = pool._states[0]
    state.healthy = True
    for _ in range(2):
        lease = await pool.acquire()
        await pool.release(lease, ok=False, kind="transport")
    assert state.failures == 2
    lease = await pool.acquire()
    await pool.release(lease, ok=True)
    assert state.failures == 0
    assert state.inflight == 0
    assert state.total == 3


@pytest.mark.asyncio
async def test_three_consecutive_failures_cordon() -> None:
    pool = make_pool("a", failure_threshold=3, cordon_s=120)
    state = pool._states[0]
    state.healthy = True
    for _ in range(3):
        lease = await pool.acquire()
        assert lease.name == "a"
        await pool.release(lease, ok=False, kind="transport")
    assert state.cordoned(time.monotonic())
    assert "consecutive failures" in state.cordon_reason


@pytest.mark.asyncio
async def test_429_cordons_until_next_window_reset() -> None:
    pool = make_pool("a")
    state = pool._states[0]
    state.healthy = True
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429")
    snap = state.snapshot(time.monotonic())
    assert snap["cordoned"] is True
    assert "free_quota" in str(snap["cordon_reason"])
    # deadline sits on the monotonic clock; remaining time must match the
    # wall-clock distance to the next 08:00 Asia/Shanghai reset.
    expected = seconds_to_window_reset()
    assert abs(state.cordon_remaining_s(time.monotonic()) - expected) < 5.0
    # a positive window: the reset is in the future
    assert expected > 0


@pytest.mark.asyncio
async def test_free_tier_rejection_cordons_immediately_and_is_skipped() -> None:
    """A 403 FreeTierError flags the egress IP: cordon on the first one.

    Measured 2026-10-17: the gate rejects by egress IP (byte-identical body
    200s from a fresh IP), so one rejection is enough — the IP cannot serve
    the free tier until the agent re-dials, and selection must stop using it
    now rather than after three consecutive failures.
    """

    pool = make_pool("a", "b", cordon_s=120)
    mark_healthy(pool)
    a, b = pool._states
    lease = await pool.acquire()
    assert lease.name == "a"
    await pool.release(lease, ok=False, kind="free_tier_rejected")
    snap = a.snapshot(time.monotonic())
    assert snap["cordoned"] is True
    assert "free_tier_rejected" in str(snap["cordon_reason"])
    assert 0 < a.cordon_remaining_s(time.monotonic()) <= 120
    # the flagged IP is skipped while the other bucket is still healthy
    lease = await pool.acquire()
    assert lease.name == "b"
    await pool.release(lease, ok=True)


@pytest.mark.asyncio
async def test_quota_cordoned_bucket_is_skipped_then_returns_after_deadline() -> None:
    pool = make_pool("a", "b")
    mark_healthy(pool)
    a, b = pool._states
    lease = await pool.acquire()
    assert lease.name == "a"
    await pool.release(lease, ok=False, kind="http_429")
    # while "a" is quota-cordoned, the healthy "b" is preferred
    lease = await pool.acquire()
    assert lease.name == "b"
    await pool.release(lease, ok=True)
    # once the wall-clock window elapses the bucket is selectable again
    a.cordoned_until = time.monotonic() - 1
    lease = await pool.acquire()
    assert lease.name == "a"


# ------------------------------------------------------------------ probes


@respx.mock
@pytest.mark.asyncio
async def test_probe_healthy_when_ip_matches_prefix() -> None:
    pool = EgressPool(
        [EgressSpec("owin10-mobile", "http://agent1:10001", "117.95.231.0/24")]
    )
    respx.get("http://agent1:10001/healthz").respond(
        json={"ready": True, "egress_ip": "117.95.231.70", "egress_ip_at": "t0"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    state = pool._states[0]  # noqa: SLF001
    assert state.healthy is True
    assert state.egress_ip == "117.95.231.70"
    assert state.last_probe_ok is True


@respx.mock
@pytest.mark.asyncio
async def test_probe_unhealthy_on_prefix_mismatch() -> None:
    pool = EgressPool(
        [EgressSpec("owin10-telecom", "http://agent2:10002", "36.149.54.0/24")]
    )
    respx.get("http://agent2:10002/healthz").respond(
        json={"ready": True, "egress_ip": "117.95.231.70"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    assert pool._states[0].healthy is True  # noqa: SLF001
    assert pool._states[0].egress_ip == "117.95.231.70"  # noqa: SLF001


@respx.mock
@pytest.mark.asyncio
async def test_probe_not_ready_marks_unhealthy() -> None:
    pool = EgressPool([EgressSpec("a", "http://agent1:10001")])
    respx.get("http://agent1:10001/healthz").respond(
        json={"ready": False, "egress_ip": "1.2.3.4"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    assert pool._states[0].healthy is False  # noqa: SLF001


@respx.mock
@pytest.mark.asyncio
async def test_probe_unhealthy_when_agent_egress_probe_failed() -> None:
    # Agent-level defence layering: an agent that still answers ready=true
    # but reports its own egress probe failing (outbound sockets broken)
    # must not receive client work (measured 2026-10-24 zombie bucket).
    pool = EgressPool([EgressSpec("a", "http://agent1:10001")])
    respx.get("http://agent1:10001/healthz").respond(
        json={"ready": True, "egress_probe_ok": False, "egress_ip": "1.2.3.4"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    assert pool._states[0].healthy is False  # noqa: SLF001
    assert pool._states[0].egress_ip == "1.2.3.4"  # noqa: SLF001


@respx.mock
@pytest.mark.asyncio
async def test_probe_healthy_with_explicit_ok_egress_probe() -> None:
    pool = EgressPool([EgressSpec("a", "http://agent1:10001")])
    respx.get("http://agent1:10001/healthz").respond(
        json={"ready": True, "egress_probe_ok": True, "egress_ip": "1.2.3.4"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    assert pool._states[0].healthy is True  # noqa: SLF001


@respx.mock
@pytest.mark.asyncio
async def test_probe_sends_agent_secret_basic_auth() -> None:
    pool = EgressPool(
        [EgressSpec("a", "http://agent1:10001")], agent_secret="s3cret"
    )
    route = respx.get("http://agent1:10001/healthz").respond(
        json={"ready": True, "egress_ip": "1.2.3.4"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    sent = route.calls.last.request
    assert sent.headers["authorization"].startswith("Basic ")


@respx.mock
@pytest.mark.asyncio
async def test_start_and_aclose_lifecycle() -> None:
    respx.get("http://agent1:10001/healthz").respond(
        json={"ready": True, "egress_ip": "1.2.3.4"}
    )
    pool = EgressPool([EgressSpec("a", "http://agent1:10001")], probe_interval_s=0.05)
    await pool.start()
    assert pool._states[0].client is not None  # noqa: SLF001
    await asyncio.sleep(0.12)
    assert pool._states[0].last_probe_ok is True  # noqa: SLF001
    await pool.aclose()
    assert pool._closed is True


# ------------------------------------------------ IP-scoped cordon release


def _capture_events(tmp_path):  # type: ignore[no-untyped-def]
    """Point EVENTS at a tmp JSONL file; returns (reader, cleanup)."""
    from proxy_opencode.egress.eventlog import EVENTS

    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))

    def read() -> list[dict]:
        with open(str(path), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    return read, lambda: EVENTS.configure("")


@respx.mock
@pytest.mark.asyncio
async def test_quota_cordon_lifted_when_isp_changes_ip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """ISP re-dial + fresh quota ⇒ the full-day 429 cordon must not persist.

    Measured 2026-10-24 (on the since-retired opc2-eth bucket): that IP exhausted
    its daily quota and was
    cordoned until the window reset; while benched the ISP re-issued
    36.149.55.24 on its own, and the fresh (healthy, in-prefix) IP kept
    serving nothing until UTC 00:00. Both 429 quota verdicts and free-tier
    gate verdicts are made against the *egress IP*: they do not carry
    over to a new address.
    """

    pool = EgressPool([EgressSpec("a", "http://agent1:10001")])
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429")
    assert state.cordoned(time.monotonic()) is True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://agent1:10001/healthz").respond(
            json={"ready": True, "egress_ip": "4.4.4.4"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.cordoned(time.monotonic()) is False
        assert state.cordon_reason == ""
        assert state.failures == 0
        assert state.healthy is True
        names = [e["event"] for e in read()]
        assert "cordon_lifted" in names
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_quota_cordon_lifted_for_out_of_prefix_ip_change(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A changed IP lifts the IP-scoped cordon even outside ``expect_prefix``.

    ``expect_prefix`` is an audit allowlist (ARCHITECTURE §6), not an
    availability gate: on 2026-10-25 owin10-telecom dialled a legitimate
    ``117.95.178.237`` that no prefix listed, so gating the lift here kept a
    perfectly healthy bucket benched until the window reset and turned 11 of
    21 rotations into recorded failures. The mismatch is reported instead.
    """

    pool = EgressPool(
        [EgressSpec("a", "http://agent1:10001", expect_prefix="3.3.3.0/24")]
    )
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429")
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://agent1:10001/healthz").respond(
            json={"ready": True, "egress_ip": "9.9.9.9"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.cordoned(time.monotonic()) is False
        assert state.cordon_reason == ""
        events = [e["event"] for e in read()]
        assert events == ["egress_ip_observed", "cordon_lifted", "prefix_mismatch"]
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_quota_cordon_not_lifted_when_probe_not_ready(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """ready=false on a changed IP: the agent is still mid-recovery."""

    pool = EgressPool([EgressSpec("a", "http://agent1:10001")])
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429")
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://agent1:10001/healthz").respond(
            json={"ready": False, "egress_ip": "4.4.4.4"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.cordoned(time.monotonic()) is True
        # ready=false is an unhealthy transition: the bucket must stop
        # receiving work immediately instead of after three request failures.
        assert state.healthy is False
        assert [e["event"] for e in read()] == [
            "egress_ip_observed",
            "probe_failed",
        ]
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_transport_cordon_not_lifted_by_ip_change(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Only the IP-scoped verdicts lift; streak cordons keep their deadline."""

    pool = EgressPool(
        [EgressSpec("a", "http://agent1:10001")], failure_threshold=3, cordon_s=9999
    )
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    for _ in range(3):
        lease = await pool.acquire()
        await pool.release(lease, ok=False, kind="transport")
    assert state.cordoned(time.monotonic()) is True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://agent1:10001/healthz").respond(
            json={"ready": True, "egress_ip": "4.4.4.4"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.cordoned(time.monotonic()) is True
        assert "consecutive failures" in state.cordon_reason
        assert [e["event"] for e in read()] == ["egress_ip_observed"]
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_free_tier_cordon_lifted_when_isp_changes_ip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The free-tier gate verdict also belongs to the old address alone."""

    pool = EgressPool([EgressSpec("a", "http://agent1:10001")])
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="free_tier_rejected")
    assert state.cordoned(time.monotonic()) is True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://agent1:10001/healthz").respond(
            json={"ready": True, "egress_ip": "4.4.4.4"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.cordoned(time.monotonic()) is False
        assert [e["event"] for e in read()] == ["egress_ip_observed", "cordon_lifted"]
        lifted = [e for e in read() if e["event"] == "cordon_lifted"][0]
        assert lifted["cordon_reason"] == "free_tier_rejected"
        assert lifted["ip_to"] == "4.4.4.4"
    finally:
        cleanup()


# ------------------------------------------------------------ proxy auth


def test_proxy_for_injects_agent_credentials_without_leaking_them() -> None:
    pool = make_pool("a")
    spec = pool._states[0].spec  # noqa: SLF001
    # No secret configured -> plain URL string, no auth object.
    assert pool._proxy_for(spec) == spec.agent_url
    authed = make_pool("a", agent_secret="s3cret")
    proxy = authed._proxy_for(authed._states[0].spec)  # noqa: SLF001
    assert isinstance(proxy, httpx.Proxy)
    assert proxy.auth == ("s3cret", "s3cret")
    # The secret must never surface in the spec/snapshot/agent_url.
    assert "s3cret" not in spec.agent_url
    assert "s3cret" not in str(authed.snapshot())


# ---------------------------------------------------------------- snapshot


def test_snapshot_shape_is_log_safe() -> None:
    pool = make_pool("a")
    state = pool._states[0]
    state.egress_ip = "1.2.3.4"
    snap = pool.snapshot()
    assert snap[0]["name"] == "a"
    assert snap[0]["egress_ip"] == "1.2.3.4"
    # no credential surface in the snapshot
    assert "secret" not in " ".join(snap[0].keys())


def test_lease_client_requires_started_egress() -> None:
    state = EgressState(spec=EgressSpec("a", "http://a:10001"))
    from proxy_opencode.egress.pool import Lease

    with pytest.raises(RuntimeError):
        _ = Lease(state).client
    state.client = httpx.AsyncClient()
    assert isinstance(Lease(state).client, httpx.AsyncClient)


# ------------------------------------------------- cordon state transitions
#
# 2026-10-25 production evidence: a drained pool re-cordoned an
# already-cordoned bucket on every subsequent 429 — 48 ERROR lines per 6h
# and ~250 `cordon` events per day for three buckets, drowning the ledger
# that is the project's only attribution tool.


@respx.mock
@pytest.mark.asyncio
async def test_repeated_429_reports_one_cordon_transition(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    read, cleanup = _capture_events(tmp_path)
    try:
        for _ in range(5):
            lease = await pool.acquire()
            await pool.release(lease, ok=False, kind="http_429", retry_after_s=27000.0)
        cordons = [e for e in read() if e["event"] == "cordon"]
        assert len(cordons) == 1
        assert cordons[0]["cordon_reason"] == "free_quota_exhausted"
        # The verdict still holds for the whole day.
        assert state.cordoned(time.monotonic()) is True
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_soft_429_cordons_for_retry_after_not_the_whole_day(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """A short Retry-After is a burst throttle, not a burned daily quota.

    Benching the bucket until UTC midnight for a 60s throttle cost it the
    rest of the day's capacity.
    """

    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        lease = await pool.acquire()
        await pool.release(lease, ok=False, kind="http_429", retry_after_s=60.0)
        assert state.cordon_reason.startswith("free_quota_soft")
        remaining = state.cordon_remaining_s(time.monotonic())
        assert 25.0 <= remaining <= 65.0
        assert seconds_to_window_reset() > 3000.0  # sanity: window is hours away
        cordons = [e for e in read() if e["event"] == "cordon"]
        assert cordons[0]["cordon_reason"] == "free_quota_soft"
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_late_hard_429_does_not_shorten_a_live_cordon() -> None:
    """A soft 429 arriving after a hard one must not reopen the bucket."""

    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429", retry_after_s=27000.0)
    before = state.cordoned_until
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429", retry_after_s=30.0)
    assert state.cordoned_until == before
    assert state.cordon_reason.startswith("free_quota_exhausted")


# ------------------------------------------------------- probe robustness
#
# 2026-10-25: a ConnectTimeout against an agent escaped to the probe loop,
# which logged a full traceback as "probe failed unexpectedly" and left
# healthy/last_probe_ok untouched — the dead bucket kept receiving client
# work until three request-level failures piled up ("10 consecutive
# failures (transport)" streaks, 305s header stalls on the same bucket).


@respx.mock
@pytest.mark.asyncio
async def test_probe_connect_timeout_marks_bucket_unhealthy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.egress_ip = "3.3.3.3"
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").mock(
            side_effect=httpx.ConnectTimeout("tailnet down")
        )
        await pool._probe_one(state)  # noqa: SLF001 - must not raise
        assert state.healthy is False
        assert state.last_probe_ok is False
        assert [e["event"] for e in read()] == ["probe_failed"]
        assert state.egress_ip == "3.3.3.3"  # nothing invented from a dead probe
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_probe_failure_logs_once_per_transition(
    tmp_path, caplog  # type: ignore[no-untyped-def]
) -> None:
    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").mock(
            side_effect=httpx.ConnectTimeout("tailnet down")
        )
        with caplog.at_level("WARNING", logger="proxy_opencode.egress"):
            for _ in range(3):
                await pool._probe_one(state)  # noqa: SLF001
        assert len([e for e in read() if e["event"] == "probe_failed"]) == 1
        assert caplog.text.count("bucket marked unhealthy") == 1
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_probe_success_keeps_request_failure_streak(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A healthy probe must not wipe request-level failures (zombie bucket).

    Measured 2026-10-27 on the esp32 bucket: the agent's control channel
    (what ``/healthz`` sees) stayed up all day while its MiFi data path
    failed ~97% of client requests. The probe succeeded every 30s and reset
    ``failures`` to 0 between requests, so the 3-consecutive-failure cordon
    could NEVER fire and the bucket kept receiving ~25% of all traffic —
    44 first hops, 43 of them 502/ECONNRESET — while looking perfectly
    healthy. Request failures are only ever wiped by a successful request
    (``release(ok=True)``) or a real agent recovery, never by a probe that
    merely confirms the control channel is alive.
    """

    pool = make_pool("a", "b", failure_threshold=3, cordon_s=120)
    mark_healthy(pool)
    a, b = pool._states
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").respond(
            json={"ready": True, "egress_ip": "1.2.3.4"}
        )
        # acquire() round-robins between the two idle buckets: land on 'a'
        # exactly twice (the second failure is what the probe must not wipe).
        landed = 0
        for _ in range(6):
            lease = await pool.acquire()
            hit_a = lease.name == "a"
            await pool.release(lease, ok=False, kind="transport")
            if hit_a:
                landed += 1
            if landed == 2:
                break
        assert a.failures == 2
        await pool._probe_one(a)  # noqa: SLF001 - the 30s probe loop
        assert a.healthy is True
        assert a.failures == 2  # streak survives the probe
        # The streak survives; each 'a' failure also emitted its demotion
        # event (0.8.5: a failing bucket is demoted, never benched) — the
        # probe itself must add nothing but the IP observation, and no
        # recovery event / failure-wipe side effect.
        assert [e["event"] for e in read() if e["egress"] == "a"] == [
            "egress_cooldown",
            "egress_cooldown",
            "egress_ip_observed",
        ]
        # ...and the third failure still cordons
        for _ in range(4):
            lease = await pool.acquire()
            hit_a = lease.name == "a"
            await pool.release(lease, ok=False, kind="transport")
            if hit_a:
                break
        assert a.cordoned(time.monotonic())
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_probe_recovery_after_outage_resets_and_emits_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Agent down -> probe fails; agent back -> one 'egress_recovered' + clean slate.

    The failure-streak reset belongs to the unhealthy->healthy *transition*
    (stale evidence about the unreachable incarnation), not to every
    successful probe.
    """

    pool = make_pool("a", failure_threshold=3, cordon_s=120)
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        for _ in range(2):
            lease = await pool.acquire()
            await pool.release(lease, ok=False, kind="transport")
        assert state.failures == 2
        # agent outage
        respx.get("http://a:10001/healthz").mock(
            side_effect=httpx.ConnectTimeout("tailnet down")
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is False
        # agent back — the streak predates the outage and is wiped exactly once
        respx.get("http://a:10001/healthz").respond(
            json={"ready": True, "egress_ip": "1.2.3.4"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is True
        assert state.failures == 0
        # a second clean probe must NOT re-emit recovery
        await pool._probe_one(state)  # noqa: SLF001
        events = [e["event"] for e in read()]
        assert events.count("egress_recovered") == 1
    finally:
        cleanup()


# ------------------------------------------------------- lease bookkeeping


@pytest.mark.asyncio
async def test_release_is_idempotent_for_the_same_lease() -> None:
    """A transport that releases and then re-raises must not double-release.

    ``inflight`` drives least-in-flight selection: a second decrement for the
    same lease silently hides another request's slot from selection.
    """

    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    lease = await pool.acquire()
    assert state.inflight == 1
    await pool.release(lease, ok=True)
    await pool.release(lease, ok=False, kind="transport")
    assert state.inflight == 0
    assert state.total == 1  # the duplicate verdict counted nothing
    assert state.failures == 0


@pytest.mark.asyncio
async def test_abandon_returns_the_slot_without_a_verdict(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Cancellation is not the bucket's fault: free the slot, count nothing.

    A leaked in-flight slot biases least-in-flight selection for the rest of
    the day, so cancellation must hand it back — but with no observer call and
    no cordon verdict, because the bucket did nothing wrong.
    """

    pool = make_pool("a", failure_threshold=1, cordon_s=120)
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    lease = await pool.acquire()
    assert state.inflight == 1
    pool.abandon(lease)
    assert state.inflight == 0
    assert state.total == 0
    assert state.failures == 0
    assert not state.cordoned(time.monotonic())
    # idempotent, like release()
    pool.abandon(lease)
    assert state.inflight == 0
    # ...and the slot is immediately reusable
    again = await pool.acquire()
    assert again.name == "a"
    assert state.inflight == 1


@respx.mock
@pytest.mark.asyncio
async def test_probe_tolerates_non_object_health_payload() -> None:
    """A JSON list from a broken agent is a failed probe, not a crash."""

    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    respx.get("http://a:10001/healthz").respond(json=["unexpected"])
    await pool._probe_one(state)  # noqa: SLF001 - must not raise
    assert state.last_probe_ok is False
    assert state.healthy is False


@pytest.mark.asyncio
async def test_quota_blackout_requires_every_bucket_to_be_benched() -> None:
    """Pool-wide blackout is an all-or-nothing verdict.

    The free tier meters the account, not the IP (measured 2026-10-29: four
    egress IPs plus a fresh MiFi IP all 429'd together), so a single healthy
    bucket means the pool can still serve and the blackout must read 0.
    """

    pool = make_pool("a", "b")
    for state in pool._states:  # noqa: SLF001
        state.healthy = True
    assert pool.quota_blackout_s() == 0.0

    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="http_429", retry_after_s=27000.0)
    # One dead, one alive: no blackout.
    assert pool.quota_blackout_s() == 0.0

    for _ in range(2):
        lease = await pool.acquire()
        if not lease.state.cordoned(time.monotonic()):
            await pool.release(lease, ok=False, kind="http_429", retry_after_s=27000.0)
        else:
            pool.abandon(lease)
    blackout = pool.quota_blackout_s()
    assert blackout > 3600.0, "both buckets benched for the daily window"


@pytest.mark.asyncio
async def test_quota_blackout_ignores_transport_cordons() -> None:
    """A bench that is not a quota verdict must not fake a blackout."""

    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    state.cordoned_until = time.monotonic() + 300.0
    state.cordon_reason = "3 consecutive failures (transport)"
    assert pool.quota_blackout_s() == 0.0


# ------------------------------------------- soft probe failures (tolerance)
#
# 2026-10-29 nightly review: the ledger held 26 probe_failed events in 5h,
# every one reading "agent not ready" and every one self-healing on the next
# 30s probe, while the buckets in question served 20/21 requests with
# failures=0. A single dropped packet on an LTE uplink (the agent probes
# ipip.net with an 8s timeout) was enough to pull a working bucket out of
# rotation, which pushes the same traffic onto fewer IPs and burns their
# daily allowance faster.


@respx.mock
@pytest.mark.asyncio
async def test_single_soft_probe_failure_tolerated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One 200-not-ready answer must not pull a working bucket (tolerance=2)."""

    pool = make_pool("a", probe_fail_tolerance=2)
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False, "egress_probe_ok": False, "egress_ip": "5.5.5.5"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is True  # still in rotation
        assert state.last_probe_ok is False  # ...but the snapshot tells the truth
        assert state.probe_soft_fail_streak == 1
        # only the first-sight egress_ip_observed; no probe_failed (not an outage)
        assert [e["event"] for e in read()] == ["egress_ip_observed"]
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_consecutive_soft_failures_pull_bucket(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Two soft failures in a row are a real verdict: pull it, once."""

    pool = make_pool("a", probe_fail_tolerance=2)
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False, "egress_ip": "5.5.5.5"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is False
        events = [e for e in read() if e["event"] == "probe_failed"]
        assert len(events) == 1
        # the reason names the field AND the streak it took to pull the bucket
        assert events[0]["reason"] == "ready=false (2/2)"
        assert state.probe_soft_fail_streak == 2
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_soft_failure_streak_resets_on_success(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A good probe between two blips must reset the streak (no slow drift)."""

    pool = make_pool("a", probe_fail_tolerance=2)
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False, "egress_ip": "5.5.5.5"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.probe_soft_fail_streak == 1
        respx.get("http://a:10001/healthz").respond(
            json={"ready": True, "egress_probe_ok": True, "egress_ip": "5.5.5.5"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.probe_soft_fail_streak == 0
        assert state.healthy is True
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False, "egress_ip": "5.5.5.5"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is True  # a fresh streak, still tolerated
        assert state.probe_soft_fail_streak == 1
        assert [e for e in read() if e["event"] == "probe_failed"] == []
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_transport_probe_failure_stays_hard_at_any_tolerance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Tolerance covers 200-not-ready only; an unreachable agent leaves at once.

    The 2026-10-25 zombie-bucket incident was exactly this: an agent whose
    tailnet path was gone kept receiving client work. A patience setting for
    LTE blips must never buy that bucket another probe interval.
    """

    pool = make_pool("a", probe_fail_tolerance=5)
    state = pool._states[0]  # noqa: SLF001
    state.healthy = True
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").mock(
            side_effect=httpx.ConnectTimeout("tailnet down")
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is False
        assert [e["event"] for e in read()] == ["probe_failed"]
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_probe_fail_reason_distinguishes_causes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """ready=false and egress_probe_ok=false need different remedies.

    Before 2026-10-29 both shipped as "agent not ready", so the nightly
    report could not tell "the device's uplink is down" (device-side) from
    "the agent state says not ready" (agent-side).
    """

    pool = make_pool("a", "b")
    read, cleanup = _capture_events(tmp_path)
    try:
        a, b = pool._states  # noqa: SLF001
        a.healthy = b.healthy = True
        respx.get("http://a:10001/healthz").respond(
            json={"ready": True, "egress_probe_ok": False, "egress_ip": "5.5.5.5"}
        )
        respx.get("http://b:10001/healthz").respond(
            json={"ready": False, "egress_ip": "6.6.6.6"}
        )
        await pool._probe_one(a)  # noqa: SLF001
        await pool._probe_one(b)  # noqa: SLF001
        reasons = {
            e["egress"]: e["reason"] for e in read() if e["event"] == "probe_failed"
        }
        assert reasons == {
            "a": "egress_probe_ok=false (1/1)",
            "b": "ready=false (1/1)",
        }
    finally:
        cleanup()


@respx.mock
@pytest.mark.asyncio
async def test_bucket_broken_at_startup_is_still_reported(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A bucket that never became healthy must still reach the ledger.

    Measured 2026-10-29: `owin10-telecom` pointed at
    ``bind_source=192.168.2.235``, an address that no longer existed on the
    host, so it failed from the first probe on. ``EgressState.healthy``
    defaults to False, there is no healthy->unhealthy transition to hook on,
    and the 0.7.x rule ("emit when was_healthy") wrote nothing — hours of a
    dead bucket looked like a clean pool in the ledger. Only
    /admin/egresses showed it. One row per outage segment is enough: the
    point is that the segment exists.
    """

    pool = make_pool("a")
    state = pool._states[0]  # noqa: SLF001
    assert state.healthy is False  # fresh pool: never been healthy
    read, cleanup = _capture_events(tmp_path)
    try:
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False, "egress_probe_ok": False, "egress_ip": "9.9.9.9"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        events = [e for e in read() if e["event"] == "probe_failed"]
        assert len(events) == 1, "startup-broken bucket must be visible once"
        assert events[0]["reason"] == "egress_probe_ok=false (1/1)"

        # ...and the same outage segment must not re-log every probe interval
        await pool._probe_one(state)  # noqa: SLF001
        await pool._probe_one(state)  # noqa: SLF001
        assert len([e for e in read() if e["event"] == "probe_failed"]) == 1

        # recovery closes the segment; a new outage opens a new one
        respx.get("http://a:10001/healthz").respond(
            json={"ready": True, "egress_probe_ok": True, "egress_ip": "9.9.9.9"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert state.healthy is True
        respx.get("http://a:10001/healthz").respond(
            json={"ready": False, "egress_probe_ok": False, "egress_ip": "9.9.9.9"}
        )
        await pool._probe_one(state)  # noqa: SLF001
        assert len([e for e in read() if e["event"] == "probe_failed"]) == 2
    finally:
        cleanup()


# ------------------------------------------------- rotation resilience (0.8.5)


@pytest.mark.asyncio
async def test_single_failure_does_not_leave_rotation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One failure must DEMOTE a bucket, never bench it (owner 2026-10-29).

    The reported defect: a bucket failed once, was cordoned, and the request
    was silently answered by a *different* account. The owner's requirement is
    the opposite: this request goes to the backup chain (proxy_ms) and the next
    request comes back to the pool — including the very bucket that failed.

    So after exactly one failure the bucket must still be selectable, still
    appear in the snapshot as not-cordoned, and only lose the *tie-break* to a
    healthy sibling.
    """

    pool = make_pool("a", "b", failure_threshold=3, cordon_s=120)
    mark_healthy(pool)
    a, b = pool._states
    read, cleanup = _capture_events(tmp_path)
    try:
        lease = await pool.acquire()
        await pool.release(lease, ok=False, kind="transport")
        failed = pool.find(lease.name)
        assert failed is not None
        failed_name = failed.spec.name
        now = time.monotonic()
        assert not failed.cordoned(now)          # still in rotation
        assert failed.cooling_down(now)          # but demoted
        snap = failed.snapshot(now)
        assert snap["cordoned"] is False
        assert snap["cooldown"] is True
        assert snap["cooldown_remaining_s"] > 0
        kinds = [e["event"] for e in read()]
        assert "egress_cooldown" in kinds
        assert "cordon" not in kinds

        # The healthy sibling is preferred while the demoted one cools down...
        for _ in range(4):
            lease = await pool.acquire()
            assert lease.name != failed_name
            await pool.release(lease, ok=True)

        # ...and one success on the demoted bucket returns it immediately.
        failed.cooldown_until = 0.0
        for _ in range(4):
            lease = await pool.acquire()
            await pool.release(lease, ok=True)
        assert pool.find(failed_name).failures == 0  # type: ignore[union-attr]
    finally:
        cleanup()


@pytest.mark.asyncio
async def test_cooldown_bucket_serves_when_it_is_the_only_candidate() -> None:
    """Demotion is a preference, not a ban: a lone cooling bucket still serves.

    This is the difference between the old cordon (which drained the pool and
    forced ``request_on_cordoned_bucket``) and the new cooldown: with the other
    buckets unhealthy the demoted one must take the request instead of idling.
    """

    pool = make_pool("a", "b", failure_threshold=3, cordon_s=120)
    a, b = pool._states
    a.healthy = True
    a.cooldown_until = time.monotonic() + 60
    b.healthy = False

    lease = await pool.acquire()
    assert lease.name == "a"


@pytest.mark.asyncio
async def test_three_failures_still_cordon() -> None:
    """Escalation is preserved: a genuinely dead leg is benched, not spun on."""

    pool = make_pool("a", failure_threshold=3, cordon_s=120)
    state = pool._states[0]
    state.healthy = True
    for _ in range(3):
        lease = await pool.acquire()
        assert lease.name == "a"
        await pool.release(lease, ok=False, kind="transport")
    now = time.monotonic()
    assert state.cordoned(now)
    assert "consecutive failures" in state.cordon_reason


@pytest.mark.asyncio
async def test_cooldown_escalates_with_streak_and_is_capped() -> None:
    """Repeated failures widen the demotion but never exceed the cap."""

    pool = make_pool("a", failure_threshold=10, cooldown_s=20, cooldown_max_s=50)
    state = pool._states[0]
    state.healthy = True
    windows = []
    for _ in range(3):
        lease = await pool.acquire()
        await pool.release(lease, ok=False, kind="transport")
        windows.append(state.cooldown_remaining_s(time.monotonic()))
    assert windows[0] < windows[1] < windows[2]
    assert max(windows) <= 50.0


@pytest.mark.asyncio
async def test_cooldown_cleared_by_success() -> None:
    pool = make_pool("a")
    state = pool._states[0]
    state.healthy = True
    lease = await pool.acquire()
    await pool.release(lease, ok=False, kind="transport")
    assert state.cooling_down(time.monotonic())
    lease = await pool.acquire()
    await pool.release(lease, ok=True)
    assert not state.cooling_down(time.monotonic())
