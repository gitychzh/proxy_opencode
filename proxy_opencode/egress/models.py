"""Egress value objects: spec, runtime state, lease, quota-window helpers.

Pure data + small invariants — no I/O, no tasks, no selection policy. Keeping
these here lets ``pool`` (policy) and ``zen.client`` (which holds leases) share
the types without the selection machinery importing the adapter layer.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

# The upstream's daily quota window resets at UTC 00:00 == 08:00 Asia/Shanghai.
_WINDOW_TZ = timezone(timedelta(hours=8))
# Public alias for the fixed-offset window timezone (shared with counters;
# counters must agree with the pool on "one window down to the second").
window_tz = _WINDOW_TZ

# A 429 whose Retry-After exceeds an hour is the upstream's per-IP daily
# allowance verdict; anything shorter is a burst throttle that clears on its
# own. Defined here (not in counters) because the pool needs it to pick the
# cordon lifetime; counters imports it for the rotation trigger so the two
# layers cannot drift apart.
HARD_QUOTA_RETRY_AFTER_S = 3600.0


def prefix_parts(expect_prefix: str | None) -> list[str]:
    """Split a CIDR prefix list, dropping blank parts.

    Accepts both commas (in-memory shapes) and semicolons: the env var
    ``ZEN_EGRESS_EXPECT_PREFIX`` is comma-separated key=value pairs, so a
    bucket's multi-CIDR value must use semicolons there; single-bucket
    construction sites can use either freely.
    """

    if not expect_prefix:
        return []
    return [
        p.strip() for p in expect_prefix.replace(";", ",").split(",") if p.strip()
    ]


class EgressSpec:
    """One egress path: name, agent URL, optional bucket-identity prefix."""

    __slots__ = ("name", "agent_url", "expect_prefix")

    def __init__(self, name: str, agent_url: str, expect_prefix: str | None = None) -> None:
        if not name or "/" in name:
            raise ValueError(f"bad egress name {name!r}")
        if not agent_url.startswith(("http://", "https://")):
            raise ValueError(f"egress {name}: agent url must be http(s): {agent_url!r}")
        for part in prefix_parts(expect_prefix):
            try:
                ipaddress.ip_network(part, strict=False)
            except ValueError as exc:
                raise ValueError(f"egress {name}: bad expect_prefix: {exc}") from exc
        self.name = name
        self.agent_url = agent_url
        self.expect_prefix = expect_prefix

    def matches_prefix(self, ip: str | None) -> bool:
        """True when the reported IP satisfies the bucket prefix.

        Accepts multi-CIDR (comma/semicolon). If no prefix configured, any
        reported IP passes — ip-matching is not a hard gate.
        """

        if not self.expect_prefix:
            return True
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(
            addr in ipaddress.ip_network(part, strict=False)
            for part in prefix_parts(self.expect_prefix)
        )


@dataclass
class EgressState:
    """Mutable runtime state for one egress."""

    spec: EgressSpec
    client: httpx.AsyncClient | None = None
    inflight: int = 0
    total: int = 0
    failures: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    cordoned_until: float = 0.0
    cordon_reason: str = ""
    # Short demotion after a transport-class failure. Owner decision
    # 2026-10-29: a bucket that fails once must NOT leave rotation — the
    # backup chain answers that single request and the very next request
    # resumes normal round-robin, including this bucket. Removing it turned
    # one flaky leg into a pool-wide outage: with three of four buckets
    # cordoned the pool drained and `request_on_cordoned_bucket` fired 114
    # times in a day, i.e. selection was forced to use the very buckets it
    # had benched. So a first failure only sinks the bucket to the BACK of
    # the pick order for `cooldown_s` (selection still considers it, so it
    # keeps serving when it is the only candidate); the escalating cooldown
    # below is what stops a genuinely dead leg from eating a timeout on
    # every turn, and three consecutive failures still trigger a real
    # cordon.
    cooldown_until: float = 0.0
    healthy: bool = False
    # P5 rotation: true while the ONT reboot orchestrator drives this egress
    # ("selection skips it; in-flight requests finish or die").
    rotating: bool = False
    egress_ip: str | None = None
    egress_ip_at: str | None = None
    last_probe_at: float = 0.0
    last_probe_ok: bool | None = None
    # Consecutive *soft* probe failures (agent answered 200 but not ready).
    # Transport failures against the agent are not counted here: those are a
    # hard verdict and still drop the bucket on the first occurrence. See
    # ``EgressPool._probe_one`` for the tolerance rule.
    probe_soft_fail_streak: int = 0
    # True once the current outage segment has been logged/emitted. Lets the
    # pool report a bucket that was already broken at process start (no
    # healthy->unhealthy transition to hook on) exactly once per segment.
    probe_alert_sent: bool = False

    def cordoned(self, now: float) -> bool:
        return self.cordoned_until > now

    def cooling_down(self, now: float) -> bool:
        """True while this bucket is demoted (still usable, picked last)."""

        return self.cooldown_until > now

    def available(self, now: float) -> bool:
        """Eligible for selection at all (not cordoned, not mid-rotation)."""

        return not self.cordoned(now) and not self.rotating

    def cordon_remaining_s(self, now: float) -> float:
        return max(0.0, self.cordoned_until - now)

    def cooldown_remaining_s(self, now: float) -> float:
        return max(0.0, self.cooldown_until - now)

    def snapshot(self, now: float) -> dict[str, object]:
        return {
            "name": self.spec.name,
            "agent_url": self.spec.agent_url,
            "healthy": self.healthy,
            "cordoned": self.cordoned(now),
            "cordon_reason": self.cordon_reason,
            "cordon_remaining_s": round(self.cordon_remaining_s(now), 1),
            "cooldown": self.cooling_down(now),
            "cooldown_remaining_s": round(self.cooldown_remaining_s(now), 1),
            "inflight": self.inflight,
            "total": self.total,
            "failures": self.failures,
            "last_error": self.last_error,
            "rotating": self.rotating,
            "egress_ip": self.egress_ip,
            "egress_ip_at": self.egress_ip_at,
            "expect_prefix": self.spec.expect_prefix,
            "egress_ip_matches_prefix": self.spec.matches_prefix(self.egress_ip),
            "last_probe_ok": self.last_probe_ok,
        }


class Lease:
    """One acquired egress; the holder releases it with success/failure."""

    __slots__ = ("state", "released", "tokens")

    def __init__(self, state: EgressState) -> None:
        self.state = state
        # Guard against a double verdict: a transport that already released
        # the lease and then re-raised must not decrement ``inflight`` twice
        # (that would under-count another request's in-flight slot).
        self.released = False
        # Estimated prompt tokens this attempt will consume upstream. The
        # adapter sets it right after acquiring (it already computed the
        # estimate for the context guard); ``pool.release`` folds it into
        # the bucket's daily token budget on success only — a 429/5xx
        # attempt costs no allowance, so it must not be charged.
        self.tokens = 0

    @property
    def name(self) -> str:
        return self.state.spec.name

    @property
    def client(self) -> httpx.AsyncClient:
        if self.state.client is None:  # pragma: no cover - start() guards this
            raise RuntimeError(f"egress {self.name} has no client")
        return self.state.client


def next_window_reset(now: datetime | None = None) -> float:
    """Unix timestamp of the next 08:00 Asia/Shanghai (upstream quota reset)."""

    now = now or datetime.now(_WINDOW_TZ)
    reset = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset.timestamp()


def seconds_to_window_reset() -> float:
    """Seconds until the next 08:00 Asia/Shanghai, for monotonic bookkeeping.

    Cordon deadlines live on the monotonic clock (robust against wall-clock
    jumps), so the quota window must be converted to a *duration* rather than
    stored as an epoch timestamp — mixing the two clocks once made a 429 cordon
    last until process restart.
    """

    return max(0.0, next_window_reset() - time.time())


__all__ = [
    "HARD_QUOTA_RETRY_AFTER_S",
    "EgressSpec",
    "EgressState",
    "Lease",
    "next_window_reset",
    "prefix_parts",
    "seconds_to_window_reset",
    "window_tz",
]
