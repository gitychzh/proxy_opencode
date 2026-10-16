"""Egress pool: per-bucket httpx clients routed through device agents.

The 0.7.x gateway runs in one Docker container on the ECS host; every
quota bucket is a device running ``egress-agent`` (an HTTP CONNECT proxy
whose outbound connections leave through that device's public IP). This
module owns:

* one ``httpx.AsyncClient`` per egress with ``proxy=<agent url>`` and
  ``trust_env=False`` (ambient HTTP_PROXY poison is a measured failure
  mode — see the zen_direct client comment);
* least-in-flight selection among healthy egresses with round-robin
  tie-breaking (mirrors the balancer) — the ties are the common case for
  sequential traffic, and breaking them by name pinned every request to
  one bucket until its daily quota drained; bounded saturation behaviour
  (when every bucket is saturated the request still proceeds — queueing
  upstream beats inventing a 502);
* cordon rules: three consecutive transport failures cordon an egress for
  a cooldown; a hard upstream 429 (per-IP daily quota) cordons it until
  the next daily window reset;
* a slow health loop polling each agent's ``GET /healthz``: it records
  the observed egress IP and enforces ``expect_prefix`` — if an agent
  reports an IP outside its bucket's prefix (measured live: after an ONT
  reboot the eth leg flaps to the wifi uplink, silently consuming another
  bucket's quota), the egress is marked unhealthy and skipped.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .eventlog import EVENTS

# The upstream's daily quota window resets at UTC 00:00 == 08:00 Asia/Shanghai.
_WINDOW_TZ = timezone(timedelta(hours=8))
# Public alias for the fixed-offset window timezone (shared with counters;
# counters must agree with the pool on "one window down to the second").
window_tz = _WINDOW_TZ

logger = logging.getLogger("proxy_opencode.egress")


class ReleaseObserver(Protocol):
    """Hook invoked by ``EgressPool.release`` after each lease release.

    Implemented by ``egress.rotate.EgressRotator`` (P5) to fold completed
    requests into the daily-window counters and to trigger ONT rotations.
    Synchronous by design: the observer may spawn tasks, but must never
    block or fail the release path (exceptions are swallowed + logged).
    """

    def on_release(
        self, name: str, *, ok: bool, kind: str, retry_after_s: float | None
    ) -> None: ...


class EgressSpec:
    """One egress path: name, agent URL, optional bucket-identity prefix."""

    __slots__ = ("name", "agent_url", "expect_prefix")

    def __init__(self, name: str, agent_url: str, expect_prefix: str | None = None) -> None:
        if not name or "/" in name:
            raise ValueError(f"bad egress name {name!r}")
        if not agent_url.startswith(("http://", "https://")):
            raise ValueError(f"egress {name}: agent url must be http(s): {agent_url!r}")
        if expect_prefix:
            try:
                ipaddress.ip_network(expect_prefix, strict=False)
            except ValueError as exc:
                raise ValueError(f"egress {name}: bad expect_prefix: {exc}") from exc
        self.name = name
        self.agent_url = agent_url
        self.expect_prefix = expect_prefix

    def matches_prefix(self, ip: str | None) -> bool:
        """True when the reported IP satisfies the bucket prefix.

        With no prefix configured any (or no) IP passes; with a prefix
        configured only an IP inside it passes — an unknown IP is NOT
        trusted, because an unverified egress could silently be leaking
        another bucket's quota.
        """

        if not self.expect_prefix:
            return True
        if not ip:
            return False
        try:
            return ipaddress.ip_address(ip) in ipaddress.ip_network(
                self.expect_prefix, strict=False
            )
        except ValueError:
            return False


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
    healthy: bool = False
    # P5 rotation: true while the ONT reboot orchestrator drives this
    # egress ("selection skips it; in-flight requests finish or die").
    rotating: bool = False
    egress_ip: str | None = None
    egress_ip_at: str | None = None
    last_probe_at: float = 0.0
    last_probe_ok: bool | None = None

    def cordoned(self, now: float) -> bool:
        return self.cordoned_until > now

    def cordon_remaining_s(self, now: float) -> float:
        return max(0.0, self.cordoned_until - now)

    def snapshot(self, now: float) -> dict[str, object]:
        return {
            "name": self.spec.name,
            "agent_url": self.spec.agent_url,
            "healthy": self.healthy,
            "cordoned": self.cordoned(now),
            "cordon_reason": self.cordon_reason,
            "cordon_remaining_s": round(self.cordon_remaining_s(now), 1),
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

    __slots__ = ("state",)

    def __init__(self, state: EgressState) -> None:
        self.state = state

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
    stored as an epoch timestamp — mixing the two clocks once made a 429
    cordon last until process restart.
    """

    return max(0.0, next_window_reset() - time.time())


class EgressPool:
    """Least-in-flight selection (round-robin ties) + health probes/cordons."""

    def __init__(
        self,
        specs: list[EgressSpec],
        *,
        agent_secret: str = "",
        timeout_s: float = 300.0,
        probe_interval_s: float = 30.0,
        cordon_s: float = 300.0,
        failure_threshold: int = 3,
        observer: ReleaseObserver | None = None,
    ) -> None:
        if not specs:
            raise ValueError("EgressPool requires at least one egress spec")
        self._states = [EgressState(spec=spec) for spec in specs]
        self._agent_secret = agent_secret
        self._timeout = httpx.Timeout(timeout_s, connect=15.0)
        self._probe_interval_s = probe_interval_s
        self._cordon_s = cordon_s
        self._failure_threshold = failure_threshold
        self._observer = observer
        self._probe_task: asyncio.Task[None] | None = None
        self._closed = False
        # Round-robin cursor for tie-breaking (see acquire).
        self._rr = itertools.count()

    @classmethod
    def from_settings(cls, settings: object) -> "EgressPool | None":
        """Build from Settings; None when no egresses are configured."""

        specs_map = getattr(settings, "zen_egress_proxies", None) or {}
        prefixes = getattr(settings, "zen_egress_expect_prefix", None) or {}
        specs = [
            EgressSpec(name, url, prefixes.get(name)) for name, url in specs_map.items()
        ]
        if not specs:
            return None
        return cls(
            specs,
            agent_secret=getattr(settings, "zen_egress_secret", "") or "",
            probe_interval_s=float(
                getattr(settings, "zen_egress_probe_interval_s", 30) or 30
            ),
            cordon_s=float(getattr(settings, "zen_egress_cordon_s", 300) or 300),
        )

    # ------------------------------------------------------------ lifecycle

    def _proxy_for(self, spec: EgressSpec) -> httpx.Proxy | str:
        """Build the httpx proxy for one agent.

        The agents require Basic auth on CONNECT (the tailnet is private but
        the proxy is still gated); credentials come from the shared
        ``agent_secret`` and are injected here rather than stored in the spec,
        so ``agent_url`` — and therefore the ``/admin/egresses`` snapshot and
        logs — never carries a secret.
        """

        if not self._agent_secret:
            return spec.agent_url
        creds = (self._agent_secret, self._agent_secret)
        return httpx.Proxy(spec.agent_url, auth=creds)

    async def start(self) -> None:
        for state in self._states:
            state.client = httpx.AsyncClient(
                proxy=self._proxy_for(state.spec),
                trust_env=False,
                timeout=self._timeout,
            )
        self._probe_task = asyncio.create_task(self._probe_loop())
        logger.info(
            "egress pool started",
            extra={"egresses": [s.spec.name for s in self._states]},
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
        for state in self._states:
            if state.client is not None:
                await state.client.aclose()

    # -------------------------------------------------------------- leasing

    async def acquire(self) -> Lease:
        """Pick the healthy egress with the least in-flight work.

        Ties are the common case (sequential traffic leaves every bucket
        idle), and they are broken by rotation rather than by name — the
        name-based order this replaced pinned every sequential request to
        the alphabetically-first bucket and drained its per-IP daily quota
        first (observed in the 2026-10-15 ECS e2e). The rotation is applied
        before the stable least-inflight sort, exactly like the balancer.
        """

        now = time.monotonic()
        ready = [
            s
            for s in self._states
            if not s.cordoned(now) and s.healthy and not s.rotating
        ]
        # Last resort excludes rotating egresses too: their ONT is mid-reboot
        # and every data connection they accept dies with it. Only when the
        # entire pool is rotating fall back to them (queueing upstream).
        non_rotating = [s for s in self._states if not s.rotating]
        usable = ready or non_rotating or list(self._states)
        offset = next(self._rr) % len(usable)
        rotated = usable[offset:] + usable[:offset]
        state = min(rotated, key=lambda s: s.inflight)
        state.inflight += 1
        if not ready and state.cordoned(now):
            # Everything is cordoned/saturated: queue upstream rather than
            # inventing a 502 (mirrors the balancer's rule).
            logger.warning(
                "no healthy egress; using cordoned bucket",
                extra={"egress": state.spec.name, "reason": state.cordon_reason},
            )
            EVENTS.emit(
                "request_on_cordoned_bucket",
                egress=state.spec.name,
                cordon_reason=state.cordon_reason[:120],
            )
        return Lease(state)

    # ------------------------------------------------------- rotation (P5)

    def attach_release_observer(self, observer: ReleaseObserver) -> None:
        """Register the release observer (builders wire the rotator late)."""

        self._observer = observer

    def find(self, name: str) -> EgressState | None:
        return next((s for s in self._states if s.spec.name == name), None)

    def mark_rotating(self, name: str, rotating: bool) -> bool:
        """Flag an egress as driving an ONT rotation; selection skips it."""

        state = self.find(name)
        if state is None or state.rotating is bool(rotating):
            return False
        state.rotating = bool(rotating)
        return True

    def apply_rotation_success(self, name: str, ip: str, ip_at: str = "") -> bool:
        """Fold a verified rotation outcome into the egress state.

        The new IP was observed on ``/healthz`` inside the bucket prefix by
        the rotator, so the state is trustworthy here: clear any cordon
        (including the per-IP daily-quota one — the fresh IP has its own
        fresh quota) and mark the egress healthy immediately, without
        waiting for the next slow probe.
        """

        state = self.find(name)
        if state is None:
            return False
        previous = state.egress_ip
        now = time.monotonic()
        state.egress_ip = ip
        if ip_at:
            state.egress_ip_at = ip_at
        state.last_probe_ok = True
        state.last_probe_at = now
        state.healthy = True
        state.failures = 0
        state.cordoned_until = 0.0
        state.cordon_reason = ""
        logger.info(
            "egress rotation complete: new egress IP live",
            extra={"egress": name, "previous_ip": previous, "current_ip": ip},
        )
        EVENTS.emit(
            "rotation_applied",
            egress=name,
            ip_from=previous,
            ip_to=ip,
        )
        return True

    async def release(
        self, lease: Lease, *, ok: bool, kind: str = "", retry_after_s: float | None = None
    ) -> None:
        """Release a lease and fold its outcome into cordon accounting."""

        state = lease.state
        state.inflight = max(0, state.inflight - 1)
        state.total += 1
        now = time.monotonic()
        if ok:
            state.failures = 0
            state.last_error = ""
            return
        state.failures += 1
        state.last_error = kind or "error"
        state.last_error_at = now
        if kind == "http_429":
            # Deadline on the monotonic clock: duration to the wall-clock
            # window reset, never the epoch timestamp itself.
            state.cordoned_until = now + seconds_to_window_reset()
            state.cordon_reason = "free_quota_exhausted (per-IP daily quota)"
            logger.error(
                "egress cordoned: upstream daily quota exhausted",
                extra={
                    "egress": state.spec.name,
                    "reset_at": datetime.fromtimestamp(
                        next_window_reset(), tz=_WINDOW_TZ
                    ).isoformat(),
                    "retry_after_s": round(retry_after_s, 1)
                    if retry_after_s is not None
                    else None,
                },
            )
            EVENTS.emit(
                "cordon",
                egress=state.spec.name,
                cordon_reason="free_quota_exhausted",
                retry_after_s=retry_after_s,
                egress_ip=state.egress_ip,
            )
        elif state.failures >= self._failure_threshold:
            state.cordoned_until = now + self._cordon_s
            state.cordon_reason = f"{state.failures} consecutive failures ({kind})"
            logger.warning(
                "egress cordoned after repeated failures",
                extra={"egress": state.spec.name, "reason": state.cordon_reason},
            )
            EVENTS.emit(
                "cordon",
                egress=state.spec.name,
                cordon_reason=state.cordon_reason[:120],
                egress_ip=state.egress_ip,
            )
        if self._observer is not None:
            try:
                self._observer.on_release(
                    state.spec.name, ok=ok, kind=kind, retry_after_s=retry_after_s
                )
            except Exception:
                # Counter/rotation bookkeeping must never break the release
                # path; the probe loop keeps serving state independently.
                logger.exception(
                    "release observer failed", extra={"egress": state.spec.name}
                )

    # --------------------------------------------------------------- probes

    async def _probe_loop(self) -> None:
        while not self._closed:
            for state in self._states:
                try:
                    await self._probe_one(state)
                except Exception:  # never let one probe kill the loop
                    logger.exception(
                        "probe failed unexpectedly", extra={"egress": state.spec.name}
                    )
            await asyncio.sleep(self._probe_interval_s)

    async def _probe_one(self, state: EgressState) -> None:
        headers = {}
        if self._agent_secret:
            import base64

            token = base64.b64encode(
                f"{self._agent_secret}:{self._agent_secret}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.get(
                f"{state.spec.agent_url.rstrip('/')}/healthz", headers=headers
            )
            payload = resp.json() if resp.status_code == 200 else {}
        now = time.monotonic()
        state.last_probe_at = now
        ok = bool(payload.get("ready")) and resp.status_code == 200
        state.last_probe_ok = ok
        ip = payload.get("egress_ip") if isinstance(payload, dict) else None
        if isinstance(ip, str) and ip:
            if ip != state.egress_ip:
                logger.info(
                    "egress ip changed",
                    extra={
                        "egress": state.spec.name,
                        "previous": state.egress_ip,
                        "current": ip,
                    },
                )
                # The probe loop is the IPs' source of truth: any change
                # (rotation, ISP re-issuing, the eth leg flapping) lands
                # here with the before/after pair for the audit trail.
                EVENTS.emit(
                    "egress_ip_observed",
                    egress=state.spec.name,
                    previous=state.egress_ip,
                    current=ip,
                    matches_prefix=state.spec.matches_prefix(ip),
                )
            state.egress_ip = ip
            state.egress_ip_at = str(payload.get("egress_ip_at") or "")
        identity_ok = state.spec.matches_prefix(state.egress_ip)
        state.healthy = ok and identity_ok
        if ok and not identity_ok:
            logger.error(
                "egress identity mismatch: agent reports an IP outside the "
                "bucket prefix (buckets are per-IP; this would silently "
                "consume another bucket's quota)",
                extra={
                    "egress": state.spec.name,
                    "egress_ip": state.egress_ip,
                    "expect_prefix": state.spec.expect_prefix,
                },
            )
            EVENTS.emit(
                "identity_mismatch",
                egress=state.spec.name,
                egress_ip=state.egress_ip,
                expect_prefix=state.spec.expect_prefix,
            )
        if state.healthy and state.failures > 0 and not state.cordoned(now):
            state.failures = 0
            state.cordon_reason = ""
            logger.info("egress recovered", extra={"egress": state.spec.name})
            EVENTS.emit("egress_recovered", egress=state.spec.name)

    # ---------------------------------------------------------------- views

    def snapshot(self) -> list[dict[str, object]]:
        now = time.monotonic()
        return [state.snapshot(now) for state in self._states]

    def names(self) -> list[str]:
        return [state.spec.name for state in self._states]


@dataclass
class PoolStats:
    """Aggregate counters for logs and healthz extensions."""

    total_requests: int = 0
    cordon_events: int = field(default=0)
