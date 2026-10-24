"""Health endpoint: listener state + current egress IP probe.

``GET /healthz`` is unauthenticated on purpose (the gateway's egress pool
polls it on a slow loop, and the payload carries no secrets — egress name,
counters, observed public IP).

The egress IP is probed from the device itself through the SAME uplink the
tunnels use (``echo_url`` from the config, bound to ``egress.bind_source``),
because the gateway pool must be able to verify which public IP a bucket
currently has — and to detect that a rotation actually changed it.

``ipip.net`` (the default echo) answers with a Chinese sentence; the probe
extracts the first token that parses as an IP address, so a plain-text
echo service works too.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from .egress import local_addr_for

log = logging.getLogger("egress_agent.health")

PROBE_INTERVAL_S = 30.0
PROBE_TIMEOUT_S = 8.0
PROBE_TTL_S = 5.0

# ``ready`` turns false this long after an unsuccessful *or* missing probe:
# the pool skips a not-ready agent instead of tunnelling through an egress
# whose outbound path is dead (measured 2026-10-24: a stale-health ready=true
# fed the bucket client requests for hours while every outbound socket timed
# out — the gateway kept handing work to a leg that returned 502 builds).
PROBE_READY_GRACE_S = PROBE_INTERVAL_S + 15.0

_IP_CANDIDATE = re.compile(r"[0-9A-Fa-f:.]+")


def extract_ip(text: str) -> str | None:
    """First substring of ``text`` that parses as an IPv4/IPv6 address."""

    for match in _IP_CANDIDATE.finditer(text):
        candidate = match.group(0)
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return None


@dataclass
class HealthState:
    """Mutable runtime state shared by the proxy, health and rotate paths."""

    egress_name: str
    bind_source: str | None
    echo_url: str
    started_at: float = field(default_factory=time.monotonic)
    tunnels_active: int = 0
    tunnels_total: int = 0
    egress_ip: str | None = None
    egress_ip_at: str | None = None
    egress_probe_ok: bool | None = None
    # Monotonic stamp of the last probe attempt (success or failure); a
    # loop that stopped firing must not serve a stale ready=true forever.
    probe_at: float = 0.0
    last_rotation_at: str | None = None
    last_rotation_ok: bool | None = None

    def payload(self) -> dict[str, object]:
        fresh = (time.monotonic() - self.probe_at) < PROBE_READY_GRACE_S
        return {
            "egress": self.egress_name,
            # True only while the egress probe loop is alive *and* the most
            # recent probe outcome is not a failure. The gateway pool skips
            # a bucket whose agent answers ready=false (its outbound sockets
            # are failing — nothing a CONNECT can use).
            "ready": self.egress_probe_ok is not False and fresh,
            "egress_ip": self.egress_ip,
            "egress_ip_at": self.egress_ip_at,
            "egress_probe_ok": self.egress_probe_ok,
            "tunnels_active": self.tunnels_active,
            "tunnels_total": self.tunnels_total,
            "uptime_s": round(time.monotonic() - self.started_at, 3),
            "last_rotation_at": self.last_rotation_at,
            "last_rotation_ok": self.last_rotation_ok,
        }

    def note_rotation(self, ok: bool) -> None:
        self.last_rotation_at = datetime.now(timezone.utc).isoformat()
        self.last_rotation_ok = ok
        if ok:
            # The old IP is stale until the next probe refreshes it.
            self.egress_ip = None
            self.egress_ip_at = None


async def probe_egress_ip(echo_url: str, bind_source: str | None) -> str | None:
    """Current public egress IP as seen from this device, or ``None``."""

    connector = aiohttp.TCPConnector(local_addr=local_addr_for(bind_source))
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(echo_url) as resp:
                text = await resp.text()
        return extract_ip(text)
    except Exception as exc:  # network flaps must never kill the probe loop
        log.warning("egress probe failed", extra={"error": type(exc).__name__})
        return None
    finally:
        await connector.close()


async def egress_probe_loop(state: HealthState, interval_s: float = PROBE_INTERVAL_S) -> None:
    """Refresh the observed egress IP in the background (never raises)."""

    while True:
        state.probe_at = time.monotonic()
        ip = await probe_egress_ip(state.echo_url, state.bind_source)
        state.egress_probe_ok = ip is not None
        if ip:
            if ip != state.egress_ip:
                log.info(
                    "egress ip observed",
                    extra={
                        "egress": state.egress_name,
                        "egress_ip": ip,
                        "previous": state.egress_ip,
                    },
                )
            state.egress_ip = ip
            state.egress_ip_at = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(interval_s)
