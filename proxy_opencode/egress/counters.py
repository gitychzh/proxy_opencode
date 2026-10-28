"""Daily-window request counters and the rotation trigger (P5).

Semantics (single unit-test contract — see the user's example):

* The window is ``[08:00, 08:00+1d)`` Asia/Shanghai (== UTC 00:00, the
  upstream's daily reset). At 08:00 every counter restarts from zero; a
  burst that spans the reset is NOT carried over (08:00-09:00 counts only
  what happened after 08:00).
* A bucket that reaches ``rotation.threshold`` completed requests inside
  the current window triggers exactly one rotation (ONT reboot via the
  agent); after the rotation the counter restarts, because the new IP is a
  fresh quota bucket.
* **The request count is the wrong meter** (measured 2026-10-28: 166
  requests burned a whole day's allowance because the upstream meters
  prompt+completion tokens, ~62k per WorkBuddy turn). ``token_budget`` is
  therefore the primary trigger: the sum of estimated prompt tokens over
  completed requests, fired before the allowance is gone. The count
  threshold stays as a coarse secondary guard.
* A hard 429 bypasses the threshold and rotates immediately: either
  ``Retry-After > 3600s`` (upstream-proven classification: that shape
  means the per-IP daily quota is exhausted) or **no Retry-After at
  all** (measured 2026-10-16: the quota 429 can come without the
  header; the real day's three quota cordons all carried
  ``retry_after_s: null`` and never rotated). Both triggers are
  deduplicated to at most one rotation per window per bucket via
  ``note_rotation_started`` setting the same ``triggered`` flag the
  threshold uses — except that a **failed** attempt may be retried on
  the next hard 429 after ``ROTATION_RETRY_COOLDOWN_S`` (2026-10-17:
  one broken ONT config must not block IP resets for the whole day).

State is in-memory plus a JSON snapshot for restart survival; the
snapshot lives outside the repo and carries no message content.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .eventlog import EVENTS
from .pool import HARD_QUOTA_RETRY_AFTER_S, next_window_reset, window_tz

logger = logging.getLogger("proxy_opencode.egress.counters")

# ADR 0.7.x: an upstream 429 whose Retry-After exceeds one hour means the
# per-IP daily quota is exhausted (soft short-window throttling also exists
# and must NOT be treated as exhaustion). Fixed by decision record; the
# constant lives in the pool so the cordon lifetime and this trigger cannot
# drift apart, and is re-exported here for callers/tests.

# A FAILED rotation attempt may be retried on the next hard 429 once this
# much time has passed (measured 2026-10-17: all three buckets failed their
# rotation — missing ONT config / prefix mismatch — and the one-per-window
# flag then kept them un-rotated for the rest of the day). A cooldown keeps
# a failing ONT from being rebooted back-to-back during a 429 burst.
ROTATION_RETRY_COOLDOWN_S = 900.0

# Floor between two hard-429 rotations of the same bucket, regardless of
# whether the previous attempt succeeded. Rationale (2026-10-25 incident):
# a 429 `FreeUsageLimitError` is produced by four different gates and only
# ONE of them (real per-IP daily quota) is cured by a new IP. Treating every
# 429 as quota rotated owin10-mobile/telecom/cc9 32/20/13 times in a single
# day while every request kept 429ing: each reboot tore down the agent's
# tunnel (probe ConnectTimeouts, one client request hung > 300 s). A real
# daily quota only needs one reset, so half an hour is generous.
HARD_ROTATE_MIN_INTERVAL_S = 1800.0

_SNAPSHOT_VERSION = 1


@dataclass
class BucketCounts:
    """Mutable counter state for one egress (per daily window)."""

    window_start: float = 0.0
    count: int = 0
    # Estimated prompt tokens folded from completed requests in this
    # window. The upstream meters tokens, not calls, so this is the
    # trigger that fires *before* the allowance is gone (2026-10-28).
    tokens: int = 0
    # Set when the threshold fired this window; cleared on success or at
    # the next 08:00 rollover. A failed rotation leaves it set so the
    # threshold cannot re-fire endlessly during a stuck window.
    triggered: bool = False
    rotations: int = 0
    last_rotation_ok: bool | None = None
    last_rotation_at: str = ""
    last_rotation_detail: str = ""
    # Wall-clock epoch of the last rotation result (drives the retry
    # cooldown for failed attempts; persisted for restart survival).
    last_rotation_epoch: float = 0.0


class EgressCounters:
    """Per-egress daily counters with rotation triggering + persistence."""

    def __init__(
        self,
        thresholds: dict[str, int] | None = None,
        *,
        token_budgets: dict[str, int] | None = None,
        hard_retry_after_s: float = HARD_QUOTA_RETRY_AFTER_S,
        retry_cooldown_s: float = ROTATION_RETRY_COOLDOWN_S,
        hard_rotate_min_interval_s: float = HARD_ROTATE_MIN_INTERVAL_S,
        snapshot_path: str | None = None,
    ) -> None:
        self._thresholds = {name: int(v) for name, v in (thresholds or {}).items()}
        self._token_budgets = {
            name: int(v) for name, v in (token_budgets or {}).items() if int(v) > 0
        }
        self._hard = float(hard_retry_after_s)
        self._retry_cooldown = float(retry_cooldown_s)
        self._hard_min_interval = max(0.0, float(hard_rotate_min_interval_s))
        self._path = snapshot_path or None
        self._records: dict[str, BucketCounts] = {}
        self.load()

    # ----------------------------------------------------------------- API

    @property
    def thresholds(self) -> dict[str, int]:
        return dict(self._thresholds)

    @property
    def token_budgets(self) -> dict[str, int]:
        return dict(self._token_budgets)

    @property
    def names(self) -> frozenset[str]:
        """Every bucket this counter meters (request and/or token trigger)."""

        return frozenset(self._thresholds) | frozenset(self._token_budgets)

    @staticmethod
    def window_start(ts: float | None = None) -> float:
        """Start of the [08:00, 08:00+1d) window containing ``ts`` (s)."""

        now_ts = ts if ts is not None else time.time()
        at = datetime.fromtimestamp(now_ts, tz=window_tz)
        return next_window_reset(at) - 86400.0

    def hard_quota(self, retry_after_s: float | None) -> bool:
        """True when a 429 classifies as per-IP daily exhaustion.

        ``Retry-After > 3600s`` is the classic signal. A missing
        Retry-After counts as exhaustion too (measured 2026-10-16: the
        upstream free tier's quota 429 can ship without the header;
        that day's three cordons all had ``retry_after_s: null``).
        Soft throttling carries a short Retry-After (≤ 3600s).
        """

        if retry_after_s is None:
            return True
        return retry_after_s > self._hard

    def _rotation_available(self, rec: BucketCounts, ts: float) -> bool:
        if not rec.triggered:
            return True
        return (
            rec.last_rotation_ok is False
            and rec.last_rotation_epoch > 0.0
            and (ts - rec.last_rotation_epoch) >= self._retry_cooldown
        )

    def should_rotate_on_429(
        self, name: str, retry_after_s: float | None, *, now: float | None = None
    ) -> bool:
        """True iff this 429 should request a rotation right now.

        Combines :meth:`hard_quota` with the per-window dedup flag so a
        hard trigger cannot stack rotations within one window. One nuance
        (measured 2026-10-17): a **failed** rotation attempt may be
        retried on the next hard 429 once ``retry_cooldown_s`` has passed
        — otherwise one broken ONT config blocks the bucket's IP reset
        for the whole day. A running attempt is still excluded (the flag
        stays latched until the result lands).

        A **successful** rotation re-arms the trigger; that is correct for
        a genuinely burned IP (new IP = fresh quota) but it also let a
        non-quota 429 storm reboot the ONT continuously (2026-10-25:
        32/20/13 reboots in one day). ``hard_rotate_min_interval_s`` is a
        floor between two hard-429 rotations of the same bucket; it only
        applies after a *successful* attempt so the failed-attempt retry
        cooldown keeps its original semantics. Zero disables the floor.
        """

        ts = now if now is not None else time.time()
        rec = self._record_for(name, ts)
        if not self.hard_quota(retry_after_s):
            return False
        if not self._rotation_available(rec, ts):
            return False
        if (
            rec.last_rotation_ok is True
            and rec.last_rotation_epoch > 0.0
            and (ts - rec.last_rotation_epoch) < self._hard_min_interval
        ):
            return False
        return True

    def should_rotate_on_free_tier(
        self, name: str, *, now: float | None = None
    ) -> bool:
        """True iff a free-tier gate rejection should request a rotation now.

        A 403 FreeTierError means the bucket's egress IP is flagged (the
        same body 200s from a fresh IP, device IPs 403 while ECS host IP 200),
        so trigger immediately — deduped to one rotation per window per bucket,
        with the failed-attempt retry cooldown so one broken ONT config cannot
        block the bucket's IP reset for the whole day.

        The hard-429 floor applies here too: a 403 storm (the shape gate
        re-tightening upstream) must not reboot an ONT every few minutes
        either. Rotating does not fix a body/shape fault, so the floor is the
        correct conservative answer.
        """

        ts = now if now is not None else time.time()
        rec = self._record_for(name, ts)
        if not self._rotation_available(rec, ts):
            return False
        if (
            rec.last_rotation_ok is True
            and rec.last_rotation_epoch > 0.0
            and (ts - rec.last_rotation_epoch) < self._hard_min_interval
        ):
            return False
        return True

    def record(
        self, name: str, *, tokens: int = 0, now: float | None = None
    ) -> bool:
        """Fold one completed (ok) request; True iff a trigger now fires.

        ``tokens`` is the estimated prompt size of that request. The
        upstream meters tokens (measured 2026-10-28: 166 calls == ~10.3M
        prompt tokens == one day's allowance), so the token budget is the
        trigger that can fire *before* the IP is burned; the request
        count stays as a coarse secondary guard.

        Both triggers fire at most once per window per bucket ("exactly
        one rotation"); the flag is cleared by :meth:`reset` (successful
        rotation, new IP = fresh quota bucket) or by the 08:00 rollover.
        """

        ts = now if now is not None else time.time()
        rec = self._record_for(name, ts)
        rec.count += 1
        if tokens > 0:
            rec.tokens += int(tokens)
        count_threshold = self._thresholds.get(name, 0)
        token_budget = self._token_budgets.get(name, 0)
        unit = ""
        if not rec.triggered:
            if count_threshold > 0 and rec.count >= count_threshold:
                unit = "requests"
            elif token_budget > 0 and rec.tokens >= token_budget:
                unit = "tokens"
        if unit:
            rec.triggered = True
            logger.warning(
                "egress rotation trigger reached: rotation requested",
                extra={
                    "egress": name,
                    "count": rec.count,
                    "tokens": rec.tokens,
                    "threshold": count_threshold,
                    "token_budget": token_budget,
                    "unit": unit,
                },
            )
        self._save()
        return bool(unit)

    def note_rotation_started(self, name: str, *, now: float | None = None) -> None:
        """Mark one rotation attempt in this window.

        Also raises the window's ``triggered`` flag so bypass triggers
        (hard 429) stay one-per-window like the threshold trigger; a
        failed rotation leaves the flag set for the rest of the window.
        """

        rec = self._record_for(name, now if now is not None else time.time())
        rec.rotations += 1
        rec.triggered = True
        self._save()

    def reset(self, name: str, *, now: float | None = None) -> None:
        """Restart the counter (successful rotation: the new IP is new quota)."""

        rec = self._record_for(name, now if now is not None else time.time())
        rec.count = 0
        rec.tokens = 0
        rec.triggered = False
        self._save()

    def note_rotation_result(
        self,
        name: str,
        *,
        ok: bool,
        detail: str = "",
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        rec = self._record_for(name, ts)
        rec.last_rotation_ok = ok
        rec.last_rotation_epoch = ts
        rec.last_rotation_at = (
            datetime.fromtimestamp(ts, tz=window_tz).isoformat(timespec="seconds")
        )
        rec.last_rotation_detail = detail[:240]
        logger.info(
            "egress rotation finished",
            extra={"egress": name, "ok": ok, "detail": detail[:160]},
        )
        self._save()

    def record_for_test(self, name: str) -> BucketCounts:  # pragma: no cover
        """Direct record access, for deterministic test setup only."""

        return self._record_for(name, time.time())

    def counts(self, name: str, *, now: float | None = None) -> BucketCounts:
        return self._record_for(name, now if now is not None else time.time())

    # ------------------------------------------------------------- snapshot

    def snapshot(self, *, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Log/admin-safe view: counts, windows and last rotation results."""

        ts = now if now is not None else time.time()
        win_start = self.window_start(ts)
        reset_at = next_window_reset(datetime.fromtimestamp(ts, tz=window_tz))
        out: dict[str, dict[str, Any]] = {}
        for name in sorted(self.names | set(self._records)):
            rec = self.counts(name, now=ts)
            out[name] = {
                "window_start": datetime.fromtimestamp(
                    rec.window_start or win_start, tz=window_tz
                ).isoformat(timespec="seconds"),
                "window": [win_start, reset_at],
                "resets_at": datetime.fromtimestamp(
                    reset_at, tz=window_tz
                ).isoformat(timespec="seconds"),
                "count": rec.count,
                "threshold": self._thresholds.get(name, 0),
                "tokens": rec.tokens,
                "token_budget": self._token_budgets.get(name, 0),
                "triggered": rec.triggered,
                "rotations": rec.rotations,
                "last_rotation_ok": rec.last_rotation_ok,
                "last_rotation_at": rec.last_rotation_at,
                "last_rotation_detail": rec.last_rotation_detail,
            }
        return out

    # ----------------------------------------------------------- persistence

    def as_json(self, *, saved_at: str | None = None) -> dict[str, Any]:
        return {
            "version": _SNAPSHOT_VERSION,
            "saved_at": saved_at
            or datetime.now(tz=window_tz).isoformat(timespec="seconds"),
            "buckets": {
                name: {
                    "window_start": rec.window_start,
                    "count": rec.count,
                    "tokens": rec.tokens,
                    "triggered": rec.triggered,
                    "rotations": rec.rotations,
                    "last_rotation_ok": rec.last_rotation_ok,
                    "last_rotation_at": rec.last_rotation_at,
                    "last_rotation_detail": rec.last_rotation_detail,
                    "last_rotation_epoch": rec.last_rotation_epoch,
                }
                for name, rec in self._records.items()
            },
        }

    def load(self, *, now: float | None = None) -> None:
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt snapshot must never block startup; counters restart
            # at zero and the per-IP quota window is upstream anyway.
            logger.error(
                "egress counters snapshot unreadable; starting fresh",
                extra={"error": type(exc).__name__},
            )
            return
        if not isinstance(payload, dict) or not isinstance(
            payload.get("buckets"), dict
        ):
            return
        for name, raw in payload["buckets"].items():
            if not isinstance(raw, dict):
                continue
            try:
                rec = BucketCounts(
                    window_start=float(raw.get("window_start") or 0.0),
                    count=int(raw.get("count") or 0),
                    tokens=int(raw.get("tokens") or 0),
                    triggered=bool(raw.get("triggered")),
                    rotations=int(raw.get("rotations") or 0),
                    last_rotation_ok=(
                        None if raw.get("last_rotation_ok") is None
                        else bool(raw.get("last_rotation_ok"))
                    ),
                    last_rotation_at=str(raw.get("last_rotation_at") or ""),
                    last_rotation_detail=str(raw.get("last_rotation_detail") or ""),
                    last_rotation_epoch=float(raw.get("last_rotation_epoch") or 0.0),
                )
            except (TypeError, ValueError):
                continue
            # Snapshots written before last_rotation_epoch existed carry
            # only the formatted timestamp: parse it back so the
            # failed-rotation retry cooldown survives the upgrade
            # (2026-10-17 live case: eth's failed rotation, epoch absent).
            if (
                rec.last_rotation_ok is False
                and rec.last_rotation_epoch == 0.0
                and rec.last_rotation_at
            ):
                try:
                    rec.last_rotation_epoch = datetime.fromisoformat(
                        rec.last_rotation_at
                    ).timestamp()
                except ValueError:
                    pass
            self._records[str(name)] = rec
        # Rows from previous windows are stale; drop their counts so a
        # restart just after 08:00 does not resurrect yesterday's total.
        win = self.window_start(now if now is not None else time.time())
        stale = [n for n, r in self._records.items() if 0 < r.window_start < win]
        for name in stale:
            self._records[name] = BucketCounts(window_start=win)
        if stale:
            logger.info(
                "dropped stale egress counter windows (post-rollover restart)",
                extra={"egresses": stale},
            )
        # Buckets that are no longer configured (a retired egress) must not
        # linger in the snapshot or the admin view forever — 2026-10-28: the
        # retired `esp32-bucket`/`ecs` rows survived every redeploy.
        retired = [n for n in self._records if n not in self.names]
        for name in retired:
            del self._records[name]
        if retired:
            logger.info(
                "dropped counter rows for buckets no longer configured",
                extra={"egresses": retired},
            )

    def save(self) -> None:
        self._save()

    def _save(self) -> None:
        if not self._path:
            return
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.as_json(), fh, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError as exc:
            # Losing a snapshot only costs four minutes of counter state
            # (worst case one extra rotation); never block the request path.
            logger.error(
                "egress counters snapshot write failed",
                extra={"error": type(exc).__name__},
            )

    # -------------------------------------------------------------- internal

    def _record_for(self, name: str, ts: float) -> BucketCounts:
        win = self.window_start(ts)
        rec = self._records.get(name)
        if rec is None:
            rec = BucketCounts(window_start=win)
            self._records[name] = rec
        elif rec.window_start != win:
            # Daily rollover at 08:00: every counter restarts from zero.
            # The burst cannot span the reset (it is metered per-IP and the
            # upstream resets its own counters at UTC 00:00 anyway).
            EVENTS.emit(
                "window_rollover",
                egress=name,
                ended_window=rec.window_start,
                final_count=rec.count,
                final_tokens=rec.tokens,
                rotations=rec.rotations,
            )
            rec.window_start = win
            rec.count = 0
            rec.tokens = 0
            rec.triggered = False
            rec.rotations = 0
        return rec
