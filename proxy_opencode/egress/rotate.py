"""Gateway-side rotation orchestration (P5).

The gateway owns the trigger (see ``counters``); this module executes it:

1. mark the egress rotating (selection skips it; in-flight requests finish
   or die — hermes' continuation semantics tolerate that);
2. ``POST {agent}/rotate`` (the agent runs the ONT driver and answers once
   the reset command was issued — see egress-agent ``rotate.py``);
3. poll the agent's ``/healthz`` until the egress is back and the IP is
   verifiably different (an unchanged IP is reported as a failed
   rotation — the ISP sometimes re-issues the same address);
4. clear the cordon, restart the bucket's counter.

All agent calls carry the egress's shared secret; no credential is ever
returned to or stored by the client side.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .counters import EgressCounters
from .eventlog import EVENTS
from .pool import EgressPool, EgressSpec

logger = logging.getLogger("proxy_opencode.egress.rotate")

# ONT reboot + PPPoE redial measured ~80s + ~30-40s on the since-retired
# opc2-eth bucket (2026-10-14);
# the wifi/cc9 ONTs may be slower. Poll well past that, in 10s steps.
ROTATE_TIMEOUT_S = 420.0
ROTATE_POLL_S = 10.0
HTTP_TIMEOUT_S = 30.0


@dataclass
class RotationOutcome:
    """Result of one rotation attempt (for logs, counters and admin)."""

    name: str
    ok: bool
    trigger: str
    detail: str = ""
    ip_from: str | None = None
    ip_to: str | None = None
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "trigger": self.trigger,
            "detail": self.detail,
            "ip_from": self.ip_from,
            "ip_to": self.ip_to,
            "elapsed_s": round(self.elapsed_s, 1),
        }


@dataclass
class IpPollResult:
    """Outcome of the wait-for-IP-change poll loop."""

    ok: bool
    detail: str
    ip: str | None = None


class EgressRotator:
    """Release observer (threshold/hard-429 triggers) + ONT rotation driver."""

    def __init__(
        self,
        pool: EgressPool,
        counters: EgressCounters,
        *,
        agent_secret: str = "",
        timeout_s: float = ROTATE_TIMEOUT_S,
        poll_s: float = ROTATE_POLL_S,
    ) -> None:
        self._pool = pool
        self._counters = counters
        self._secret = agent_secret
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        # One rotation task per bucket at a time; a trigger fired while one
        # is already running is a no-op (the trigger re-arms via counters).
        self._tasks: dict[str, asyncio.Task[RotationOutcome]] = {}
        self._names = frozenset(counters.thresholds)
        # Wiring order in the app factory: the pool exists first, then the
        # rotator attaches itself as the pool's release observer.
        pool.attach_release_observer(self)

    @classmethod
    def from_settings(cls, settings: Any, pool: EgressPool) -> "EgressRotator | None":
        """Build from Settings; None when no bucket is configured for rotation."""

        thresholds = getattr(settings, "zen_egress_rotation", None) or {}
        if not thresholds:
            return None
        counters = EgressCounters(
            dict(thresholds),
            hard_rotate_min_interval_s=float(
                getattr(settings, "zen_egress_rotate_min_interval_s", 1800.0)
            ),
            snapshot_path=getattr(settings, "zen_egress_counts_path", None) or None,
        )
        return cls(
            pool,
            counters,
            agent_secret=getattr(settings, "zen_egress_secret", "") or "",
        )

    @property
    def counters(self) -> EgressCounters:
        return self._counters

    def rotatable(self, name: str) -> bool:
        return name in self._names

    # --------------------------------------------------- observer interface

    def on_release(
        self, name: str, *, ok: bool, kind: str, retry_after_s: float | None
    ) -> None:
        """Fold one release outcome in and fire the rotation trigger.

        * completed (ok) request → counters; at the per-window threshold
          exactly one rotation is requested (a failed attempt leaves the
          flag set until the next window rolls over at 08:00).
        * upstream 429 whose Retry-After classifies as per-IP daily
          quota exhaustion (measured boundary: > 3600s, or no
          Retry-After header at all — both, 2026-10-16 measurement)
          → immediate rotation, bypassing the threshold, at most one
          per window per bucket; a FAILED attempt may be retried after
          the counters' retry cooldown so a transient ONT/agent problem
          does not block IP resets for the rest of the day. Soft
          throttles (< 3600s, with a short Retry-After) keep the plain
          pool cordon and do not rotate.
        """

        if ok:
            if self._counters.record(name):
                self.request_rotation(name, trigger="threshold")
        elif kind == "http_429" and self._counters.should_rotate_on_429(
            name, retry_after_s
        ):
            logger.warning(
                "upstream 429 classifies as per-IP daily quota exhausted: "
                "rotating now (bypassing request threshold)",
                extra={"egress": name, "retry_after_s": retry_after_s},
            )
            self.request_rotation(name, trigger="hard_429")
        elif kind == "free_tier_rejected" and self._counters.should_rotate_on_free_tier(
            name
        ):
            logger.warning(
                "free-tier gate flagged the egress IP: rotating now",
                extra={"egress": name},
            )
            self.request_rotation(name, trigger="free_tier_rejected")

    # ------------------------------------------------------------ triggering

    def request_rotation(
        self, name: str, *, trigger: str
    ) -> asyncio.Task[RotationOutcome] | None:
        """Start one rotation for ``name`` (at most one task per bucket).

        Returns the task, or None when the bucket has no ONT configured or
        a rotation is already running. Non-rotatable buckets are rejected
        outright (e.g. the ECS host itself runs an agent but must never
        reboot anything).
        """

        if not self.rotatable(name):
            logger.warning(
                "rotation requested for a bucket without ONT config",
                extra={"egress": name, "trigger": trigger},
            )
            return None
        if self.running(name):
            logger.info(
                "rotation already running", extra={"egress": name, "trigger": trigger}
            )
            return None
        EVENTS.emit("rotation_requested", egress=name, trigger=trigger)
        self._counters.note_rotation_started(name)
        self._pool.mark_rotating(name, True)
        self._tasks[name] = asyncio.get_running_loop().create_task(
            self._rotate(name, trigger)
        )
        return self._tasks[name]

    def running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    async def aclose(self) -> None:
        """Cancel in-flight rotations (gateway shutdown)."""

        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        for name, task in self._tasks.items():
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    self._counters.note_rotation_result(
                        name, ok=False, detail="cancelled at gateway shutdown"
                    )
                    EVENTS.emit(
                        "rotation_cancelled", egress=name, detail="gateway shutdown"
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error(
                        "rotation task failed at shutdown",
                        extra={"egress": name, "error": type(exc).__name__},
                    )
                    raise
        self._tasks.clear()

    # ------------------------------------------------------------- execution

    async def _rotate(self, name: str, trigger: str) -> RotationOutcome:
        state = self._pool.find(name)
        if state is None:  # pragma: no cover - guarded by request_rotation
            self._pool.mark_rotating(name, False)
            return RotationOutcome(
                name=name, ok=False, trigger=trigger, detail="unknown egress"
            )
        started = time.monotonic()
        state0 = state.egress_ip
        EVENTS.emit("rotation_started", egress=name, trigger=trigger, ip_from=state0)
        outcome = await self._rotate_flow(state.spec, trigger)
        outcome.elapsed_s = time.monotonic() - started
        if outcome.ok:
            self._pool.apply_rotation_success(name, outcome.ip_to or "")
            # New IP = fresh quota bucket: restart the counter inside the
            # current window, so the *new* address gets its own threshold.
            self._counters.reset(name)
            EVENTS.emit(
                "rotation_succeeded",
                egress=name,
                trigger=trigger,
                ip_from=outcome.ip_from or state0,
                ip_to=outcome.ip_to,
                elapsed_s=round(outcome.elapsed_s, 1),
            )
        else:
            EVENTS.emit(
                "rotation_failed",
                egress=name,
                trigger=trigger,
                ip_from=outcome.ip_from or state0,
                detail=outcome.detail[:200],
                elapsed_s=round(outcome.elapsed_s, 1),
            )
        self._counters.note_rotation_result(name, ok=outcome.ok, detail=outcome.detail)
        self._pool.mark_rotating(name, False)
        return outcome

    async def _rotate_flow(self, spec: EgressSpec, trigger: str) -> RotationOutcome:
        name = spec.name
        state = self._pool.find(name)
        ip_from = state.egress_ip if state is not None else None
        agent = await self._agent_rotate(spec)
        if not agent["ok"] and not agent.get("unreachable"):
            return RotationOutcome(
                name=name,
                ok=False,
                trigger=trigger,
                detail=f"agent rotate refused: {agent['detail']}",
                ip_from=ip_from,
            )
        # An unreachable POST (transport error after the request was sent,
        # or no route to the agent at all) does NOT mean the rotation died:
        # the agent may already be executing the reboot, or the ONT may be
        # re-dialing on its own. Poll for the recovery under the caller's
        # deadline and fold whatever IP actually lands. A definite refusal
        # (HTTP error status, ok=false body) above really is final.
        poll = await self._wait_for_ip_change(
            spec, ip_from, deadline=time.monotonic() + self._timeout_s
        )
        if poll.ok:
            detail = poll.detail
            if not agent["ok"]:
                detail = f"agent unreachable during call, but {poll.detail}"
            return RotationOutcome(
                name=name,
                ok=True,
                trigger=trigger,
                detail=detail,
                ip_from=ip_from,
                ip_to=poll.ip,
            )
        if not agent["ok"]:
            return RotationOutcome(
                name=name,
                ok=False,
                trigger=trigger,
                detail=f"agent rotate unreachable: {agent['detail']}; {poll.detail}",
                ip_from=ip_from,
            )
        return RotationOutcome(
            name=name, ok=False, trigger=trigger, detail=poll.detail, ip_from=ip_from
        )

    async def _agent_rotate(self, spec: EgressSpec) -> dict[str, Any]:
        """POST the agent's control endpoint; ``{"ok": bool, "detail": str}``.

        The agent runs the P1 four-step Huawei ONT reboot (production-
        verified on the since-retired opc2-eth) and answers once the reset command
        went out;
        the gateway itself waits out the ~80s recovery + ~40s PPPoE redial
        by polling ``/healthz``. A transport-level failure carries
        ``unreachable=True``: the command may still have gone out (or the
        ONT is independently re-dialing), so the caller keeps polling
        instead of declaring the rotation dead — observed 2026-10-24:
        the (since-retired) opc2-eth rotated fine during a tunnel flap that killed
        the POST.
        """

        url = f"{spec.agent_url.rstrip('/')}/rotate"
        headers = self._auth_headers()
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=httpx.Timeout(HTTP_TIMEOUT_S, connect=10.0)
            ) as client:
                resp = await client.post(url, json={"wait": False}, headers=headers)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "detail": f"agent unreachable: {exc}",
                "unreachable": True,
            }
        detail = ""
        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            pass
        if isinstance(payload, dict):
            results = payload.get("results")
            first = results[0] if isinstance(results, list) and results else {}
            detail = str(
                (first or {}).get("detail") or payload.get("detail") or ""
            )[:160]
            ok = resp.status_code == 200 and bool(payload.get("ok"))
        else:
            ok = False
        return {"ok": ok, "detail": detail or f"status {resp.status_code}", "unreachable": False}

    async def _wait_for_ip_change(
        self, spec: EgressSpec, ip_from: str | None, *, deadline: float
    ) -> IpPollResult:
        """Poll /healthz until the egress is back with a changed IP.

        An unchanged IP (the ISP sometimes re-issues the same address) is
        a failed rotation: logged loudly, recorded in the counters, and
        the operator can re-trigger via the admin API.

        A *changed* IP outside ``expect_prefix`` is accepted and flagged.
        The prefix is a manually maintained audit allowlist (ARCHITECTURE
        §6), and enforcing it as a hard precondition cost real availability:
        on 2026-10-25 owin10-telecom's legitimate ``117.95.178.237`` and
        several ``49.x``/``121.234.x`` addresses were not listed, so 11 of
        21 rotations were recorded as failures and the bucket stayed
        cordoned for the rest of the day. The measured failure the check
        was built for (the retired opc2 device's eth leg silently falling back to
        its wifi
        uplink) cannot happen on the Windows buckets, which pin the source
        address with ``bind_source``.
        """

        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_s)
            ip = await self._probe_ip(spec)
            if not ip:
                continue  # mid-reboot the agent has no egress IP yet
            if ip == ip_from:
                continue
            if not spec.matches_prefix(ip):
                logger.warning(
                    "rotated egress ip is outside the configured prefix "
                    "(audit only; refresh ZEN_EGRESS_EXPECT_PREFIX)",
                    extra={"egress": spec.name, "ip": ip},
                )
                EVENTS.emit("prefix_mismatch", egress=spec.name, egress_ip=ip)
            return IpPollResult(
                ok=True,
                detail=f"egress ip {ip_from or '?'} -> {ip}",
                ip=ip,
            )
        return IpPollResult(
            ok=False,
            detail=f"egress ip unchanged ({ip_from}) after {self._timeout_s:.0f}s",
        )

    async def _probe_ip(self, spec: EgressSpec) -> str | None:
        headers = self._auth_headers()
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(HTTP_TIMEOUT_S / 3, connect=5.0),
            ) as client:
                resp = await client.get(
                    f"{spec.agent_url.rstrip('/')}/healthz", headers=headers
                )
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("ready"):
            ip = payload.get("egress_ip")
            if isinstance(ip, str) and ip:
                return ip
        return None

    def _auth_headers(self) -> dict[str, str]:
        if not self._secret:
            return {}
        token = base64.b64encode(f"{self._secret}:{self._secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
