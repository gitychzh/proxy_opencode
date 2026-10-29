"""Egress pool policy: selection, cordon, health probes, lease accounting.

The value objects it operates on live in :mod:`.models` (spec / state / lease /
quota-window helpers); this module decides *which* bucket serves a request and
*what* a failed attempt means for that bucket:

* one ``httpx.AsyncClient`` per egress with ``proxy=<agent url>`` and
  ``trust_env=False`` (ambient HTTP_PROXY poison is a measured failure mode);
* least-in-flight selection among healthy egresses with round-robin
  tie-breaking — ties are the common case for sequential traffic, and breaking
  them by name pinned every request to one bucket until its daily quota
  drained; when every bucket is saturated the request still proceeds
  (queueing upstream beats inventing a 502);
* cordon rules: three consecutive transport failures cordon a bucket for a
  cooldown; a hard upstream 429 (per-IP daily quota) cordons it until the next
  daily window reset; an IP-scoped verdict is lifted when the IP changes;
* a slow health loop polling each agent's ``GET /healthz``, recording the
  observed egress IP and reporting ``expect_prefix`` mismatches (audit only —
  enforcing that allowlist deadlocked the pool on 2026-10-25).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from datetime import datetime
from typing import Protocol

import httpx

from .eventlog import EVENTS
from .models import (
    HARD_QUOTA_RETRY_AFTER_S,
    EgressSpec,
    EgressState,
    Lease,
    next_window_reset,
    seconds_to_window_reset,
    window_tz,
)

logger = logging.getLogger("proxy_opencode.egress")

# Cordon verdicts that mean "this account's allowance is gone", as opposed to
# a broken tunnel. When every bucket carries one of these, the pool is in a
# blackout: no attempt can succeed, so selection short-circuits instead of
# paying an RTT per dead egress.
_QUOTA_REASONS = frozenset({"free_quota_exhausted", "free_quota_soft"})


class ReleaseObserver(Protocol):
    """Hook invoked by ``EgressPool.release`` after each lease release.

    Implemented by ``egress.rotate.EgressRotator`` (P5) to fold completed
    requests into the daily-window counters and to trigger ONT rotations.
    Synchronous by design: the observer may spawn tasks, but must never block
    or fail the release path (exceptions are swallowed + logged).
    """

    def on_release(
        self,
        name: str,
        *,
        ok: bool,
        kind: str,
        retry_after_s: float | None,
        tokens: int = 0,
    ) -> None: ...


class EgressPool:
    """Least-in-flight selection (round-robin ties) + health probes/cordons."""

    # Cordon verdicts that are anchored to the *egress IP* rather than to
    # the bucket's own agent: the upstream judges quota (429) and the
    # free-tier gate (403) per source address, so a freshly ISP-issued IP
    # inherits neither — see ``_lift_ip_scoped_cordon``.
    _IP_SCOPED_CORDON_REASONS = frozenset(
        {"free_quota_exhausted", "free_tier_rejected"}
    )

    def __init__(
        self,
        specs: list[EgressSpec],
        *,
        agent_secret: str = "",
        timeout_s: float = 300.0,
        probe_interval_s: float = 30.0,
        cordon_s: float = 300.0,
        failure_threshold: int = 3,
        cooldown_s: float = 20.0,
        cooldown_max_s: float = 60.0,
        probe_fail_tolerance: int = 1,
        observer: ReleaseObserver | None = None,
        last_resort: frozenset[str] = frozenset(),
    ) -> None:
        if not specs:
            raise ValueError("EgressPool requires at least one egress spec")
        self._states = [EgressState(spec=spec) for spec in specs]
        self._agent_secret = agent_secret
        self._timeout = httpx.Timeout(timeout_s, connect=15.0)
        self._probe_interval_s = probe_interval_s
        self._cordon_s = cordon_s
        self._failure_threshold = failure_threshold
        # Demotion window for a failing-but-not-dead bucket (see
        # _enter_cooldown). Kept short and bounded: the bucket stays in
        # rotation, it only loses tie-breaks to healthy siblings.
        self._cooldown_s = max(1.0, float(cooldown_s))
        self._cooldown_max_s = max(self._cooldown_s, float(cooldown_max_s))
        # Consecutive soft probe failures tolerated before a bucket is pulled
        # (see _probe_one). Never below 1: a tolerance of 0 would mean "pull
        # the bucket before the first failure", which is the same as 1.
        self._probe_fail_tolerance = max(1, probe_fail_tolerance)
        self._observer = observer
        # Buckets that may only absorb overflow (see ZEN_EGRESS_LAST_RESORT).
        # Unknown names are ignored so a retired bucket cannot silently make
        # the whole pool last-resort.
        known = {s.spec.name for s in self._states}
        self._last_resort = frozenset(n for n in last_resort if n in known)
        self._probe_task: asyncio.Task[None] | None = None
        self._probe_timeout = httpx.Timeout(10.0, connect=5.0)
        self._probe_client: httpx.AsyncClient | None = None
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
            cooldown_s=float(
                getattr(settings, "zen_egress_cooldown_s", 20) or 20
            ),
            cooldown_max_s=float(
                getattr(settings, "zen_egress_cooldown_max_s", 60) or 60
            ),
            probe_fail_tolerance=int(
                getattr(settings, "zen_egress_probe_fail_tolerance", 1) or 1
            ),
            last_resort=frozenset(
                getattr(settings, "zen_egress_last_resort", frozenset()) or frozenset()
            ),
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
        # Probes talk to the agents directly over the tailnet (never through
        # a bucket's own proxy), so they get their own connection pool.
        self._probe_client = httpx.AsyncClient(
            trust_env=False, timeout=self._probe_timeout
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
        if self._probe_client is not None:
            await self._probe_client.aclose()
            self._probe_client = None
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
        before the stable least-inflight sort.

        Ordering (owner decision 2026-10-29 — a failing bucket must not
        leave rotation):

          1. normal healthy buckets;
          2. buckets in a *cooldown* (one recent failure): still candidates,
             just picked after every healthy sibling, so the next request
             tries a different account first and this one serves again as
             soon as it is needed;
          3. last-resort buckets, withheld while any normal bucket can
             serve (a weak uplink that cannot meet the header deadline);
          4. cordoned buckets, only when nothing else is left (queueing
             upstream beats inventing a 502).

        The tiers are cumulative: a bucket appears in exactly one, and the
        first non-empty tier wins. This is why a single transport failure no
        longer parks a third of the pool.
        """

        now = time.monotonic()
        healthy = [
            s for s in self._states if s.available(now) and s.healthy
        ]
        cooling = [s for s in healthy if s.cooling_down(now)]
        normal = [s for s in healthy if not s.cooling_down(now)]
        # Last-resort buckets (a weak uplink that cannot meet the first-byte
        # deadline) are withheld as long as any normal bucket can serve: an
        # equal share of round-robin picks turned every fourth turn into a
        # 60s TTFB stall followed by a failover (measured 2026-10-29).
        ready = [s for s in normal if s.spec.name not in self._last_resort]
        # Everything cordoned/unhealthy: queue upstream rather than inventing
        # a 502. Rotating egresses stay excluded while any alternative
        # exists — their ONT is mid-reboot and every data connection they
        # accept dies with it.
        benched = [s for s in self._states if s.available(now)]
        tiers = [
            ready,
            # A cooling bucket is still a healthy sibling of the same speed
            # class, so it outranks a last-resort uplink that cannot meet the
            # header deadline anyway.
            [s for s in cooling if s.spec.name not in self._last_resort],
            [s for s in normal if s.spec.name in self._last_resort],
            cooling,
            benched,
            list(self._states),
        ]
        usable = next((t for t in tiers if t), list(self._states))
        offset = next(self._rr) % len(usable)
        rotated = usable[offset:] + usable[:offset]
        state = min(rotated, key=lambda s: s.inflight)
        state.inflight += 1
        if state.cordoned(now):
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

    def quota_blackout_s(self, now: float | None = None) -> float:
        """Seconds of *pool-wide* quota blackout, or 0 when any bucket can serve.

        The upstream's free tier meters an account, not an IP (measured
        2026-10-29: four egress IPs plus the ECS's own IP plus a freshly
        rebooted MiFi IP all answered ``FreeUsageLimitError`` within minutes).
        So when every bucket carries a quota verdict there is nothing left to
        try: the caller should answer 429 immediately and let the fallback
        chain take over instead of paying one upstream RTT per dead bucket.
        """

        now = time.monotonic() if now is None else now
        if not self._states:
            return 0.0
        remaining: list[float] = []
        for state in self._states:
            core = state.cordon_reason.split(" (", 1)[0]
            if not state.cordoned(now) or core not in _QUOTA_REASONS:
                return 0.0
            remaining.append(state.cordon_remaining_s(now))
        return min(remaining)

    def find(self, name: str) -> EgressState | None:
        return next((s for s in self._states if s.spec.name == name), None)

    @property
    def size(self) -> int:
        """Number of configured buckets (failover budget source)."""

        return len(self._states)

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
        state.cooldown_until = 0.0
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
        """Release a lease and fold its outcome into cordon accounting.

        Idempotent: the second call for the same lease is a no-op, so a
        transport that released and then re-raised cannot corrupt the
        bucket's in-flight accounting.
        """

        state = lease.state
        if lease.released:
            logger.debug(
                "lease already released; ignoring duplicate verdict",
                extra={"egress": state.spec.name, "reason": kind},
            )
            return
        lease.released = True
        state.inflight = max(0, state.inflight - 1)
        state.total += 1
        now = time.monotonic()
        if ok:
            state.failures = 0
            state.last_error = ""
            # A success is the only proof the leg works again: drop the
            # demotion immediately so the bucket returns to its normal share
            # of rotation on the very next pick.
            state.cooldown_until = 0.0
            # The ok path used to return before the observer ran, which
            # meant counters.record() never saw a single completed request
            # and the rotation threshold could not fire (found 2026-10-17:
            # every bucket showed count=0 after a full day of 200s). The
            # release outcome must reach the trigger logic on ok too.
            self._notify_observer(
                state, ok, kind, retry_after_s, tokens=lease.tokens
            )
            return
        state.failures += 1
        state.last_error = kind or "error"
        state.last_error_at = now
        if kind == "http_429":
            # Soft throttling (short Retry-After) is NOT a daily-quota
            # verdict: benching the bucket until UTC midnight for a 30s
            # throttle used to cost the bucket the rest of the day. Only the
            # hard shape (Retry-After > 1h, or none at all) means the
            # account's allowance is gone.
            #
            # Scope (corrected 2026-10-29 02:00 CST): the allowance is
            # ACCOUNT-scoped, not per-IP. Four egress IPs, the ECS's own IP
            # and a freshly rebooted MiFi IP (111.55.72.119 -> 111.55.74.1)
            # all returned FreeUsageLimitError within minutes of each other,
            # with the same Retry-After pointing at the 08:00 window reset.
            # Rotation therefore buys a fresh rate-limit bucket, never new
            # quota; only the reset does.
            soft_s = (
                retry_after_s
                if retry_after_s is not None and retry_after_s <= HARD_QUOTA_RETRY_AFTER_S
                else None
            )
            if soft_s is not None:
                self._enter_cordon(
                    state,
                    now + max(30.0, float(soft_s)),
                    "free_quota_soft (short Retry-After)",
                    event_reason="free_quota_soft",
                    detail={"retry_after_s": round(float(soft_s), 1)},
                )
            else:
                self._enter_cordon(
                    state,
                    now + seconds_to_window_reset(),
                    "free_quota_exhausted (account daily quota)",
                    event_reason="free_quota_exhausted",
                    detail={
                        "reset_at": datetime.fromtimestamp(
                            next_window_reset(), tz=window_tz
                        ).isoformat(),
                        "retry_after_s": round(retry_after_s, 1)
                        if retry_after_s is not None
                        else None,
                    },
                )
        elif kind == "free_tier_rejected":
            # The free-tier gate flagged this bucket's egress IP: it cannot
            # serve the free tier until the agent re-dials (measured 2026-10-17:
            # byte-identical body 200s from a fresh IP, 403 from flagged IP).
            # Cordon it now so selection skips it while the rotator re-dials; a
            # successful rotation clears the cordon immediately.
            self._enter_cordon(
                state,
                now + self._cordon_s,
                "free_tier_rejected (egress IP flagged by gate)",
                event_reason="free_tier_rejected",
                detail={"egress_ip": state.egress_ip},
            )
        elif state.failures >= self._failure_threshold:
            # Genuinely dead leg (three consecutive failures): bench it for
            # the full cooldown so it cannot eat a header deadline every
            # turn. A SINGLE failure never reaches this branch — it is only
            # demoted below, which keeps the bucket in rotation (owner
            # decision 2026-10-29).
            self._enter_cordon(
                state,
                now + self._cordon_s,
                f"{state.failures} consecutive failures ({kind})",
                event_reason=None,
                detail={"egress_ip": state.egress_ip},
            )
        else:
            # First (and second) failure: demote, do not remove.
            #
            # Owner decision 2026-10-29: "不要移出轮换". A single transport
            # hiccup on one leg used to bench that bucket outright; with
            # four buckets, three cordons drained the pool and selection was
            # forced onto benched buckets (114 `request_on_cordoned_bucket`
            # events in one day). The correct shape is: this request is
            # answered by the backup chain, this bucket sinks to the back of
            # the pick order for a short cooldown, and the next request
            # resumes normal round-robin — including this bucket once it is
            # the best candidate again.
            self._enter_cooldown(state, now, kind)
        self._notify_observer(state, ok, kind, retry_after_s)

    def _enter_cooldown(self, state: EgressState, now: float, kind: str) -> None:
        """Demote a bucket for a short window without leaving rotation.

        The cooldown grows with the consecutive-failure streak so a leg that
        is genuinely down stops being the first pick (one header deadline
        per turn), while a one-off blip costs exactly one demotion. It is
        capped well below the cordon window: this is a preference hint, not
        a health verdict.
        """

        streak = max(1, state.failures)
        seconds = min(
            self._cooldown_s * streak,
            self._cooldown_max_s,
        )
        state.cooldown_until = max(state.cooldown_until, now + seconds)
        logger.info(
            "egress demoted (cooldown, still in rotation)",
            extra={
                "egress": state.spec.name,
                "reason": kind,
                "count": streak,
                "elapsed_s": round(seconds, 1),
            },
        )
        EVENTS.emit(
            "egress_cooldown",
            egress=state.spec.name,
            reason=kind,
            count=streak,
            elapsed_s=round(seconds, 1),
        )

    def abandon(self, lease: Lease) -> None:
        """Drop a lease with NO verdict about the bucket.

        Used when the request went away before any upstream byte reached the
        client (client disconnect / task cancellation): the bucket is not at
        fault, so nothing is counted — not a success, not a failure, no
        observer call. Only the in-flight slot is returned, which is what
        keeps least-in-flight selection honest for the rest of the day: a
        cancelled attempt used to leak the slot until the process restarted.
        """

        if lease.released:
            return
        lease.released = True
        lease.state.inflight = max(0, lease.state.inflight - 1)

    def _enter_cordon(
        self,
        state: EgressState,
        until: float,
        reason: str,
        *,
        event_reason: str | None,
        detail: dict[str, object],
    ) -> None:
        """Cordon a bucket, reporting only real state transitions.

        Every 429 used to re-cordon and re-emit at ERROR even though the
        bucket was already benched, so a drained pool produced 48 ERROR
        lines per 6h and ~250 ``cordon`` events per day for three buckets —
        the event ledger, which is the project's only attribution tool,
        drowned in duplicates. Repeated verdicts now extend the deadline
        silently (DEBUG) and only a fresh transition logs/emits.
        """

        core = reason.split(" (", 1)[0]
        now_mono = time.monotonic()
        live = state.cordoned(now_mono)
        already = live and state.cordon_reason.split(" (", 1)[0] == core
        # Never shorten or relabel a live cordon: a soft throttle arriving
        # after a daily-quota verdict must not turn a full-day bench into a
        # 30s cooldown (nor mislabel what the bucket is waiting for).
        if already or (live and until <= state.cordoned_until):
            if not already:
                logger.debug(
                    "shorter cordon verdict ignored",
                    extra={"egress": state.spec.name, "reason": core},
                )
                return
            state.cordoned_until = max(state.cordoned_until, until)
            logger.debug(
                "cordon extended (already cordoned)",
                extra={"egress": state.spec.name, "reason": core},
            )
            return
        state.cordoned_until = until
        state.cordon_reason = reason
        if core == "free_quota_exhausted":
            logger.error(
                "egress cordoned: upstream daily quota exhausted",
                extra={"egress": state.spec.name, **detail},
            )
        elif core == "free_quota_soft":
            logger.warning(
                "egress cordoned: upstream soft throttle",
                extra={"egress": state.spec.name, "reason": reason, **detail},
            )
        elif core == "free_tier_rejected":
            logger.error(
                "egress cordoned: free-tier gate flagged this egress IP",
                extra={"egress": state.spec.name, **detail},
            )
        else:
            logger.warning(
                "egress cordoned after repeated failures",
                extra={"egress": state.spec.name, "reason": reason, **detail},
            )
        EVENTS.emit(
            "cordon",
            egress=state.spec.name,
            cordon_reason=event_reason or reason[:120],
            **detail,
        )

    def _notify_observer(
        self,
        state: EgressState,
        ok: bool,
        kind: str,
        retry_after_s: float | None,
        *,
        tokens: int = 0,
    ) -> None:
        """Fold one release outcome into the rotator/counters (never fatal)."""

        if self._observer is None:
            return
        try:
            self._observer.on_release(
                state.spec.name,
                ok=ok,
                kind=kind,
                retry_after_s=retry_after_s,
                tokens=tokens,
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

    def _lift_ip_scoped_cordon(self, state: EgressState, new_ip: str) -> None:
        """Undo cordons whose reason was a verdict about the previous IP.

        Both quota exhaustion (429 ``FreeUsageLimitError``) and the
        free-tier gate (403 ``FreeTierError``) are per-source-address
        verdicts (measured 2026-10-17: byte-identical bodies accept a
        fresh IP, reject the flagged one). The rotation driver already
        clears cordon on a verified rotation; this closes the remaining
        gap: the ISP re-dials on its own while the bucket sits on a
        full-day quota cordon — observed 2026-10-24 on the since-retired opc2
        bucket, which re-dialed
        after its rotate call was unreachable and the healthy new
        ``36.149.55.24`` stayed benched until the window reset.

        Preconditions: the probe just succeeded (``ok``), the IP really
        changed, and the cordon reason is one of the IP-scoped verdicts.
        Anything else (transport streaks, plain cooldowns) keeps its
        deadline; a fresh IP that is also exhausted will re-cordon on its
        own first 429.

        ``expect_prefix`` is deliberately NOT a precondition here (it is an
        audit field, per ARCHITECTURE §6). Gating availability on a
        hand-maintained CIDR allowlist deadlocked the pool: on 2026-10-25
        owin10-telecom dialled a legitimate ``117.95.178.237`` that no
        prefix listed, so its cordon could never lift and 11 of 21
        rotations were recorded as failures while the bucket served fine.
        A mismatch is reported instead of enforced.
        """

        reason = state.cordon_reason.split(" (", 1)[0]
        if reason not in self._IP_SCOPED_CORDON_REASONS:
            return
        state.cordoned_until = 0.0
        state.cordon_reason = ""
        state.failures = 0
        in_prefix = state.spec.matches_prefix(new_ip)
        logger.info(
            "cordon lifted: egress IP changed, the old verdict does not carry over",
            extra={
                "egress": state.spec.name,
                "cordon_reason": reason,
                "ip": new_ip,
                "matches_prefix": in_prefix,
            },
        )
        EVENTS.emit(
            "cordon_lifted",
            egress=state.spec.name,
            cordon_reason=reason,
            ip_to=new_ip,
            matches_prefix=in_prefix,
        )
        if not in_prefix:
            # Keep the stale allowlist visible without letting it decide
            # availability: operators need to refresh expect_prefix.
            logger.warning(
                "egress ip outside the configured prefix (audit only)",
                extra={"egress": state.spec.name, "ip": new_ip},
            )
            EVENTS.emit(
                "prefix_mismatch", egress=state.spec.name, egress_ip=new_ip
            )

    async def _probe_one(self, state: EgressState) -> None:
        headers = {}
        if self._agent_secret:
            import base64

            token = base64.b64encode(
                f"{self._agent_secret}:{self._agent_secret}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"
        # One long-lived client for every probe (three buckets every 30s used
        # to open a fresh connection pool each round, which both churned
        # sockets on the tailnet and made a connect timeout indistinguishable
        # from a programming error).
        client = self._probe_client
        try:
            if client is None:  # pragma: no cover - start() always sets it
                async with httpx.AsyncClient(
                    trust_env=False, timeout=self._probe_timeout
                ) as one_off:
                    resp = await one_off.get(
                        f"{state.spec.agent_url.rstrip('/')}/healthz", headers=headers
                    )
                    payload = resp.json() if resp.status_code == 200 else {}
            else:
                resp = await client.get(
                    f"{state.spec.agent_url.rstrip('/')}/healthz", headers=headers
                )
                payload = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as exc:
            # A transport failure against the agent IS the health verdict.
            # Previously the exception escaped to the probe loop, which
            # logged a full traceback as "probe failed unexpectedly" and left
            # ``healthy``/``last_probe_ok`` untouched: a dead agent kept
            # receiving client work until three request-level failures piled
            # up (2026-10-25: "10 consecutive failures (transport)" streaks
            # and 305s header stalls on the same bucket).
            self._mark_probe_failed(state, type(exc).__name__)
            return
        if not isinstance(payload, dict):
            # Any non-object JSON (a list, a bare string) is a broken agent,
            # not a crash: treating it as an empty payload keeps the loop on
            # its normal healthy/unhealthy path instead of raising
            # AttributeError out of ``payload.get`` below.
            payload = {}
        now = time.monotonic()
        state.last_probe_at = now
        ok = bool(payload.get("ready")) and resp.status_code == 200
        # Layered defence with the agent's own readiness: an agent that
        # answers ready=true but reports a failed egress probe
        # (``egress_probe_ok=false`` — outbound sockets failing) must not
        # receive client work; older agents that omit the field keep the
        # previous behaviour (default true).
        if payload.get("egress_probe_ok") is False:
            ok = False
        previous_ok = state.healthy
        state.last_probe_ok = ok
        ip = payload.get("egress_ip") if isinstance(payload, dict) else None
        ip_changed = False
        current_ip = ""
        if isinstance(ip, str) and ip:
            current_ip = ip
            ip_changed = ip != state.egress_ip
            if ip_changed:
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
        # ip-matching is informational only (do not gate health)
        if not ok:
            # A 200 answer that says "not ready" is a *soft* verdict: the
            # agent is up and talking, only its outbound leg is momentarily
            # unusable (the agent itself probes ipip.net every 30s with an
            # 8s timeout, so one dropped packet on an LTE uplink is enough).
            # Measured 2026-10-29: 13 such flaps in 5h, each self-healed on
            # the very next probe, none backed by a request-level failure
            # (those buckets served 20/21 requests with failures=0).
            # Tolerating a few of them keeps the bucket in rotation, which
            # spreads traffic over more IPs and slows daily-allowance burn.
            # Transport failures / non-200 stay HARD (handled above): a
            # bucket whose agent is unreachable must leave rotation at once
            # (2026-10-25 zombie-bucket incident).
            self._probe_soft_failure(
                state,
                self._probe_fail_reason(resp.status_code, payload),
                was_healthy=previous_ok,
            )
            return
        state.healthy = True
        state.probe_soft_fail_streak = 0
        state.probe_alert_sent = False
        if ip_changed:
            # Only lift on a *proven-good* probe: a ready=false answer with
            # a fresh IP means the agent is still mid-recovery.
            self._lift_ip_scoped_cordon(state, current_ip)
        if previous_ok is False:
            logger.info("egress agent reachable again", extra={"egress": state.spec.name})
            EVENTS.emit("egress_recovered", egress=state.spec.name)
            # The agent just came back from a probe-level outage: any failure
            # streak it accumulated while it was down is stale evidence about
            # the old (unreachable) incarnation, so give it a clean slate.
            if not state.cordoned(now):
                state.failures = 0
                state.cordon_reason = ""

    @staticmethod
    def _probe_fail_reason(status_code: int, payload: dict[str, object]) -> str:
        """Name the exact field a not-ready answer came from.

        2026-10-29 review: every flap in the ledger read ``agent not ready``,
        which is the same string for "agent admits its own outbound probe
        failed" and "agent reports ready=false" — the two have different
        remedies (device uplink vs. agent state), and the ambiguity made the
        nightly report unable to attribute them. Keep both spellings stable so
        findings can group by them.
        """

        if status_code != 200:
            return f"status {status_code}"
        if payload.get("egress_probe_ok") is False:
            return "egress_probe_ok=false"
        return "ready=false"

    def _probe_soft_failure(
        self, state: EgressState, reason: str, *, was_healthy: bool
    ) -> None:
        """Fold a soft (200-but-not-ready) probe failure into health state.

        The bucket leaves rotation only once ``ZEN_EGRESS_PROBE_FAIL_TOLERANCE``
        consecutive soft failures pile up; below that the streak is logged as
        INFO (no WARNING, no ``probe_failed`` event) so a single LTE blip does
        not show up as a pool outage. ``last_probe_ok`` still reports the raw
        truth — the snapshot must not claim a broken probe succeeded.
        """

        state.probe_soft_fail_streak += 1
        streak = state.probe_soft_fail_streak
        suffix = f" ({streak}/{self._probe_fail_tolerance})"
        if streak < self._probe_fail_tolerance:
            logger.info(
                "egress probe soft failure (tolerated)",
                extra={"egress": state.spec.name, "reason": reason + suffix},
            )
            return
        self._mark_probe_failed(state, reason + suffix, was_healthy=was_healthy)

    def _mark_probe_failed(
        self, state: EgressState, reason: str, *, was_healthy: bool | None = None
    ) -> None:
        """Fold an unreachable/unready agent into the health state.

        One WARNING line per transition (an agent that stays down must not
        re-log every 30s) and no traceback: a connect timeout is a normal
        operating condition on this network, not a defect.
        """

        state.last_probe_at = time.monotonic()
        state.last_probe_ok = False
        if was_healthy is None:
            was_healthy = state.healthy
        state.healthy = False
        # A bucket that is broken from process start never transitions
        # healthy->unhealthy, so the 0.7.x rule below wrote nothing at all:
        # `owin10-telecom` sat at bind_source=192.168.2.235 (an address that
        # no longer existed on the host) for hours while the ledger showed a
        # perfectly clean pool — the fact only surfaced through
        # /admin/egresses. `probe_alert_sent` makes the FIRST failure of an
        # outage segment visible regardless of the previous state, while
        # still logging once per segment (not every 30s).
        if was_healthy or not state.probe_alert_sent:
            state.probe_alert_sent = True
            logger.warning(
                "egress agent probe failed: bucket marked unhealthy",
                extra={"egress": state.spec.name, "reason": reason[:80]},
            )
            EVENTS.emit("probe_failed", egress=state.spec.name, reason=reason[:80])

    # ---------------------------------------------------------------- views

    def snapshot(self) -> list[dict[str, object]]:
        now = time.monotonic()
        rows = []
        for state in self._states:
            row = state.snapshot(now)
            row["last_resort"] = state.spec.name in self._last_resort
            rows.append(row)
        return rows

    def names(self) -> list[str]:
        return [state.spec.name for state in self._states]
