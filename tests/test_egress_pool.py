"""Tests for the 0.7.x egress pool (selection, cordon, probes, config)."""

from __future__ import annotations

import asyncio
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
            "opc2-wifi": "http://100.0.0.1:10001",
            "opc2-eth": "http://100.0.0.1:10002",
        },
        zen_egress_expect_prefix={"opc2-wifi": "117.95.231.0/24"},
        zen_egress_secret="s3cret",
    )
    pool = EgressPool.from_settings(settings)
    assert pool is not None
    assert pool.names() == ["opc2-wifi", "opc2-eth"]
    wifi = pool._states[0].spec  # noqa: SLF001
    assert wifi.agent_url == "http://100.0.0.1:10001"
    assert wifi.expect_prefix == "117.95.231.0/24"
    assert pool._states[1].spec.expect_prefix is None  # noqa: SLF001


def test_empty_pool_rejected() -> None:
    with pytest.raises(ValueError):
        EgressPool([])


# --------------------------------------------------------------- selection


@pytest.mark.asyncio
async def test_acquire_picks_least_inflight_and_ties_by_name() -> None:
    pool = make_pool("a", "b")
    mark_healthy(pool)
    l1 = await pool.acquire()
    assert l1.name == "a"  # tie -> deterministic by name
    l2 = await pool.acquire()
    assert l2.name == "b"  # a now has one in flight
    await pool.release(l1, ok=True)
    l3 = await pool.acquire()
    assert l3.name == "a"


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
        [EgressSpec("opc2-wifi", "http://agent1:10001", "117.95.231.0/24")]
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
    # Measured live: after an ONT reboot the eth leg flapped to the wifi
    # uplink; the pool must refuse to route that bucket's quota away.
    pool = EgressPool(
        [EgressSpec("opc2-eth", "http://agent2:10002", "36.149.54.0/24")]
    )
    respx.get("http://agent2:10002/healthz").respond(
        json={"ready": True, "egress_ip": "117.95.231.70"}
    )
    await pool._probe_one(pool._states[0])  # noqa: SLF001
    assert pool._states[0].healthy is False  # noqa: SLF001


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
