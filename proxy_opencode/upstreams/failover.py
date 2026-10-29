"""Backup-upstream failover wrapper: zen-direct primary -> proxy_ms (GLM-5.3-Flash).

Owner requirement (2026-10-25, extended 2026-10-26): when the zen free-tier
chain fails — quota 429, 5xx, transport failure, free-tier gate 403/503,
*timeout* (header deadline, whole-request deadline, or a stream that never
produces its first chunk) — the gateway must switch to the ModelScope
aggregation gateway (``proxy_ms``, same ECS host, :10003, serving
``ZhipuAI/GLM-5.3-Flash``) automatically and conspicuously, then come back.

Design:
  * Wrapper adapter around the primary (built in ``build_adapter``). All
    three protocol routes plus /v1/models consume ``app.state.adapter``, so
    one wrapper covers every surface.
  * Failover triggers: any exception raised by the primary (transport
    error, timeout, adapter fault) or an upstream error relay whose status
    is >= 400, EXCEPT the explicit client-fault statuses in
    ``_PASSTHROUGH_STATUSES`` — a malformed request or an unknown model
    replayed on another vendor is still malformed, and retrying it only
    doubles the client's latency.
  * Deadlines: ``ZEN_FALLBACK_PRIMARY_TIMEOUT_S`` bounds the whole primary
    attempt (0 = off), and ``ZEN_FALLBACK_FIRST_CHUNK_TIMEOUT_S`` prefetches
    the first SSE chunk so a stream that never starts still fails over
    *before* any byte reaches the client (0 = off). Once the first chunk is
    out the turn is committed: a mid-stream failure is the stream
    watchdog's business, never a silent retry that would duplicate output.
  * Failure scope (owner decision 2026-10-29): the egress buckets are treated
    as independent accounts, so **one bucket failing costs one request**. That
    request is answered by the backup chain, the pool cordons the failing
    bucket, and the next request resumes normal round-robin on a sibling
    bucket (``ZEN_FALLBACK_REQUEST_SCOPED``, default true). The circuit
    breaker is then reserved for a genuine pool-wide verdict — a quota
    blackout deadline from every account at once — and holds the backup for
    ``ZEN_FALLBACK_HOLD_S`` (or the blackout deadline, capped at one day),
    half-open probing afterwards and closing after
    ``ZEN_FALLBACK_RECOVER_AFTER`` consecutive primary successes.
  * Prominent notifications (red-line compliant: no content, no keys):
      - the FIRST trip of an outage is a production incident: ERROR
        "PRODUCTION INCIDENT - FALLBACK ENGAGED" (cannot be filtered away
        by INFO-tuned log collection) plus ``severity: incident`` on the
        JSONL event; a re-trip inside the same outage is the incident
        continuing (INFO, ``severity: continuation``)
      - recovery closes the incident: WARNING "FALLBACK RECOVERED" +
        ``severity: recovered``
      - per-request INFO "FALLBACK ROUTING" with the rewritten model
      - structured JSONL events via the shared egress event sink
      - response headers ``X-Gateway-Fallback`` /
        ``X-Gateway-Fallback-Model`` / ``X-Gateway-Fallback-Reason`` and a
        leading SSE comment on streamed answers served by the backup
      - /healthz grows a "fallback" block (engaged / hold remaining / model)
  * Model ids: the secondary speaks ModelScope ids, not zen bare ids.
    ``ZEN_FALLBACK_MODEL_MAP`` (``from=to`` csv) rewrites the request model
    before handing the payload over; ``ZEN_FALLBACK_DEFAULT_MODEL`` catches
    everything unmapped (production: ``ZhipuAI/GLM-5.3-Flash``).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import replace
from typing import Any, AsyncIterator

from ..config import Settings
from ..egress.eventlog import EVENTS
from ..errors import InvalidRequest
from .openai_http import OpenAIHttpAdapter, StreamRelay

logger = logging.getLogger("proxy_opencode.failover")

# Statuses relayed to the client untouched instead of triggering the backup:
# the request body (or the requested model id / key) is wrong, so another
# vendor cannot serve it either.
_PASSTHROUGH_STATUSES = frozenset({400, 401, 404, 422})

# Upper bound on a quota-driven breaker hold. The free tier resets on a daily
# window, so a Retry-After larger than a day is either a bug or a different
# policy; never pin the chain to the backup longer than that.
_QUOTA_HOLD_CAP_S = 24 * 3600.0

# Client-visible provenance marker (owner requirement 2026-10-26: the
# failover must be *noticeable*, not just present in the logs). Routes
# promote it to response headers; streams also carry a leading SSE comment.
FALLBACK_HEADER = "X-Gateway-Fallback"
FALLBACK_MODEL_HEADER = "X-Gateway-Fallback-Model"
FALLBACK_REASON_HEADER = "X-Gateway-Fallback-Reason"
FALLBACK_NAME = "proxy_ms"

# Marker of the in-flight request's fallback decision. ``dispatch`` awaits
# ``chat()`` in the same task, so the value set here is visible to the route
# that built the request and to nobody else (asyncio gives every task its own
# context copy) — safe under concurrency.
_MARKER: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "proxy_opencode_fallback_marker", default=None
)


def current_marker() -> dict[str, Any] | None:
    """Marker of the in-flight request's fallback decision (None = primary)."""

    return _MARKER.get()


def fallback_headers() -> dict[str, str]:
    """HTTP headers announcing that the backup chain served this answer."""

    marker = current_marker()
    if not marker:
        return {}
    return {
        FALLBACK_HEADER: str(marker.get("secondary") or FALLBACK_NAME),
        FALLBACK_MODEL_HEADER: str(marker.get("model") or ""),
        FALLBACK_REASON_HEADER: str(marker.get("reason") or "")[:120],
    }


def fallback_note() -> str:
    """SSE comment line (ignored by every SSE parser) naming the backup."""

    marker = current_marker()
    if not marker:
        return ""
    return (
        f": gateway-fallback secondary={marker.get('secondary') or FALLBACK_NAME}"
        f" model={marker.get('model') or ''}"
        f" reason={(marker.get('reason') or '')[:120]}\n\n"
    )


def _is_error_relay(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("__status__"), int)


def _backup_error_relay(
    upstream_status: int, secondary: str, reason: str
) -> dict[str, Any]:
    """Reshape a failed backup answer into a gateway-owned retryable 503.

    The backup's own error (e.g. proxy_ms billing 429) must never reach the
    client wearing the primary chain's identity: Hermes classifies a 429
    carrying ModelScope's billing text as ``layer: billing / retryable:
    false`` and stops, and ops read it as a zen quota burn worth an IP
    rotation — neither of which is true here. The shape is the gateway's
    own (``fallback_backup_error``), explicitly retryable, with the backup
    chain and the original upstream status named for attribution; the
    upstream body is not echoed (it may name upstream model ids and the
    route layer only masks success-shaped bodies).
    """
    return {
        "__status__": 503,
        "content": {
            "error": {
                "message": (
                    f"primary upstream unavailable ({reason[:120]}); backup "
                    f"gateway {secondary} also failed with HTTP {upstream_status}"
                ),
                "type": "api_error",
                "param": None,
                "code": "fallback_backup_error",
            }
        },
    }


class _PrefetchedRelay(StreamRelay):
    """A StreamRelay whose first SSE chunk was already pulled.

    Exists so a streaming turn that stalls before its first body byte can be
    retried on the backup *before* the route starts the response; once the
    first chunk has been handed over the turn is committed. Subclasses
    ``StreamRelay`` so every route's ``isinstance(result, StreamRelay)``
    branch keeps working unchanged.
    """

    def __init__(
        self,
        inner: StreamRelay,
        first: bytes,
        chunks: AsyncIterator[bytes],
    ) -> None:
        super().__init__(inner.response, inner.usage_holder, stall_s=0.0)
        self._inner = inner
        self._first = first
        self._chunks = chunks

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            yield self._first
            async for chunk in self._chunks:
                yield chunk
        finally:
            # Closing the iterator runs StreamRelay.chunks' own finally, which
            # releases the upstream response and fires the pool close hook.
            aclose = getattr(self._chunks, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # pragma: no cover - teardown must not mask exit
                    pass


class FailoverAdapter:
    """Serve chat through ``primary``; fail over to proxy_ms on any failure."""

    def __init__(
        self,
        primary: Any,
        settings: Settings,
        secondary: Any | None = None,
    ) -> None:
        self._primary = primary
        self.name = primary.name
        self._model_map = dict(settings.zen_fallback_model_map)
        self._default_model = settings.zen_fallback_default_model
        self._hold_s = max(1.0, float(settings.zen_fallback_hold_s))
        # Secondary client: the plain OpenAI passthrough adapter pointed at
        # the backup gateway (proxy_ms), reusing its stream relay semantics.
        # Tests inject a pre-built secondary instead.
        self._secondary: Any = (
            OpenAIHttpAdapter(
                replace(
                    settings,
                    upstream_base_url=settings.zen_fallback_base_url,
                    upstream_api_key=settings.zen_fallback_api_key,
                )
            )
            if secondary is None
            else secondary
        )
        # Breaker state (monotonic clock): 0 = closed (primary active).
        self._fallback_until = 0.0
        self._secondary_name = settings.zen_fallback_base_url
        # Hysteresis for the closing edge. A single primary success used to
        # close the breaker, so a primary that served ~half of its requests
        # produced engaged/recovered churn at request rate (2026-10-25: 57
        # engaged / 11 recovered events in a day, six round trips in 20
        # minutes). Each cycle also switched the model the client was served
        # by, since the backup speaks a different id.
        self._recover_after = max(1, int(settings.zen_fallback_recover_after))
        self._primary_streak = 0
        self._last_trip_at = 0.0
        # Request-scoped failover (default on): a single bucket failure is
        # answered by the backup for that request only; the hold breaker is
        # reserved for a pool-wide quota blackout (owner 2026-10-29).
        self._request_scoped = bool(
            getattr(settings, "zen_fallback_request_scoped", True)
        )
        # 0 disables the corresponding guard.
        self._primary_timeout_s = max(
            0.0, float(settings.zen_fallback_primary_timeout_s)
        )
        self._first_chunk_timeout_s = max(
            0.0, float(settings.zen_fallback_first_chunk_timeout_s)
        )

    # ------------------------------------------------------------ status

    def fallback_status(self) -> dict[str, Any]:
        """/healthz block: is the backup chain currently serving?"""
        remaining = self._fallback_until - time.monotonic()
        return {
            "engaged": remaining > 0,
            "secondary": self._secondary_name,
            "model": str(self._default_model or ""),
            "hold_remaining_s": round(remaining, 1) if remaining > 0 else 0,
            "scope": "request" if self._request_scoped else "hold",
        }

    # ------------------------------------------------------------- proxy

    async def list_models(self) -> Any:
        try:
            return await self._primary.list_models()
        except Exception:
            # /v1/models degrades to the backup catalogue only when the
            # primary is unreachable; the route layer shapes errors anyway.
            return await self._secondary.list_models()

    async def chat(self, payload: dict[str, Any]) -> Any:
        _MARKER.set(None)
        if time.monotonic() < self._fallback_until:
            # Breaker open: skip the (recently failed) primary entirely.
            return await self._via_secondary(payload, "hold window")
        try:
            result = await self._call_primary(payload)
        except InvalidRequest:
            # A request-level fault is the CLIENT's, not the chain's: the
            # prompt is too long, the body is malformed, and the backup would
            # answer the very same bad request from a different model. Tripping
            # on it opened the breaker for every other client and — measured in
            # production 2026-10-28 — replayed an oversized prompt against
            # proxy_ms while logging a PRODUCTION INCIDENT. Re-raise so the
            # route answers 400 with its own code (context_length_exceeded).
            raise
        except Exception as exc:  # transport, timeout, adapter fault
            return await self._trip(payload, _describe(exc))
        status = result.get("__status__") if _is_error_relay(result) else None
        if isinstance(status, int) and status >= 400:
            if status in _PASSTHROUGH_STATUSES:
                return result
            # A pool-wide quota blackout carries the time left until the
            # allowance resets. Holding the breaker for the flat 120s window
            # re-probed a primary that could not possibly serve for hours:
            # every 2 minutes one unlucky request paid the dead-pool walk and
            # was then answered by a different model anyway (measured 7.25s
            # for a 16-token reply, 2026-10-29 02:10 CST).
            blackout = (
                result.get("__quota_blackout_s__") if _is_error_relay(result) else None
            )
            return await self._trip(
                payload, f"upstream HTTP {status}", blackout_s=blackout
            )
        # The primary answered this request. Past the hold window that makes
        # this request a half-open probe; the breaker only closes once
        # ``_recover_after`` consecutive probes have succeeded.
        self._primary_streak += 1
        if self._fallback_until and self._primary_streak >= self._recover_after:
            self._fallback_until = 0.0
            self._primary_streak = 0
            self._announce_recovery()
        return result

    # ---------------------------------------------------------- internals

    async def _call_primary(self, payload: dict[str, Any]) -> Any:
        """Call the primary under the request deadline, prefetching SSE.

        The deadline covers everything up to "the adapter handed us a
        result": for a streaming turn that includes the first body byte,
        because the prefetch below pulls it. That is exactly where the
        client's clock starts and the last moment a retry is invisible.
        """
        call = self._primary.chat(payload)
        if self._primary_timeout_s > 0:
            result = await asyncio.wait_for(call, timeout=self._primary_timeout_s)
        else:
            result = await call
        if not isinstance(result, StreamRelay) or self._first_chunk_timeout_s <= 0:
            return result
        return await self._prefetch(result)

    async def _prefetch(self, relay: StreamRelay) -> StreamRelay:
        """Pull the first SSE chunk so a stalled stream can still fail over."""

        chunks = relay.chunks()
        try:
            first = await asyncio.wait_for(
                chunks.__anext__(), timeout=self._first_chunk_timeout_s
            )
        except BaseException:
            # The generator body may already have started, so aclose() runs
            # the relay's pool-release hook. A first-chunk timeout is treated
            # as a primary fault by the caller (see _trip), and the lease must
            # be returned as a *failure* or the bucket's health would look
            # fine after a stall.
            relay.end_reason = "stalled"
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # pragma: no cover - best-effort teardown
                    pass
            raise
        logger.debug(
            "stream first chunk prefetched",
            extra={"bytes": len(first), "timeout_s": self._first_chunk_timeout_s},
        )
        return _PrefetchedRelay(relay, first, chunks)

    def _map_model(self, requested: str) -> str:
        mapped = self._model_map.get(requested) or self._default_model
        return str(mapped or "")

    async def _trip(
        self,
        payload: dict[str, Any],
        reason: str,
        *,
        blackout_s: float | None = None,
    ) -> Any:
        hold_s = self._hold_s
        if blackout_s is not None:
            # Cap so a bogus Retry-After cannot pin the chain to the backup
            # for days; the free tier's window is at most a day.
            hold_s = max(hold_s, min(float(blackout_s), _QUOTA_HOLD_CAP_S))
        # Request-scoped failover (owner decision 2026-10-29, buckets are four
        # independent accounts): the failure of ONE bucket must not park the
        # client on the backup. Only a pool-wide verdict — every account out
        # of quota, reported as a blackout deadline — is worth a hold; a
        # single-account fault is answered from the backup for THIS request
        # and the next request goes back to the pool's round-robin (which
        # already skips the cordoned bucket).
        hold_open = blackout_s is not None or not self._request_scoped
        if hold_open:
            self._fallback_until = time.monotonic() + hold_s
        self._primary_streak = 0
        # Distinguish the first trip of an outage from the re-trips that a
        # flapping primary produces: the former is an incident (WARNING), the
        # latter is the same incident continuing (INFO). Both keep their
        # JSONL event so attribution is unchanged.
        now = time.monotonic()
        re_trip = self._last_trip_at > 0.0 and (now - self._last_trip_at) < (
            hold_s * 2
        )
        self._last_trip_at = now
        if re_trip:
            logger.info(
                "FALLBACK RE-ENGAGED: primary upstream %s failed again (%s); "
                "this request is served by the backup chain%s",
                self.name,
                reason,
                f" for the next {hold_s:.0f}s"
                if hold_open
                else "; the next request retries the pool",
            )
        elif hold_open:
            # Production-incident grade: the primary chain just died and
            # clients are being served by a different model. ERROR so the
            # line survives even INFO-filtered log collection, and a greppable
            # "PRODUCTION INCIDENT" prefix the operator can alert on.
            logger.error(
                "PRODUCTION INCIDENT - FALLBACK ENGAGED: primary upstream %s "
                "unavailable (%s); serving via backup gateway %s for the "
                "next %.0fs",
                self.name,
                reason,
                FALLBACK_NAME,
                hold_s,
            )
        else:
            # One bucket, one request: the pool cordons that account and the
            # next request is already scheduled for a sibling. WARNING, not
            # incident — this is routine failover, not an outage.
            logger.warning(
                "FALLBACK SERVED: bucket of primary upstream %s failed (%s); "
                "this request is answered by backup gateway %s, the next one "
                "resumes pool rotation",
                self.name,
                reason,
                FALLBACK_NAME,
            )
        EVENTS.emit(
            "fallback_engaged",
            severity=(
                "continuation"
                if re_trip
                else ("incident" if hold_open else "request")
            ),
            primary=self.name,
            reason=reason[:120],
            hold_s=hold_s if hold_open else 0.0,
            quota_blackout=blackout_s is not None,
            request_scoped=not hold_open,
            secondary=self._secondary_name,
            model=self._map_model(str(payload.get("model") or "")),
            re_trip=re_trip,
        )
        return await self._via_secondary(payload, reason)

    async def _via_secondary(self, payload: dict[str, Any], reason: str) -> Any:
        body = dict(payload)
        requested = str(body.get("model") or "")
        mapped = self._map_model(requested)
        if mapped:
            body["model"] = mapped
        # Client-visible marker: routes promote it to response headers and
        # streams to a leading SSE comment, so a switched answer is never
        # mistaken for a primary answer.
        _MARKER.set(
            {
                "secondary": FALLBACK_NAME,
                "model": body.get("model") or "",
                "requested_model": requested,
                "reason": reason,
                "hold_s": self._hold_s,
            }
        )
        logger.info(
            "FALLBACK ROUTING: model %r -> %r via %s (%s)",
            requested,
            body.get("model"),
            self._secondary_name,
            reason,
        )
        try:
            result = await self._secondary.chat(body)
        except Exception as exc:
            # Backup also unreachable: surface it loudly, nothing else to try.
            # BOTH chains are dead — the incident just got worse, so this is
            # ERROR with the incident prefix (not a mere warning).
            logger.error(
                "PRODUCTION INCIDENT - FALLBACK FAILED: backup gateway %s "
                "unreachable (%s); primary was down (%s)",
                self._secondary_name,
                exc,
                reason,
            )
            EVENTS.emit(
                "fallback_failed",
                severity="incident",
                primary=self.name,
                secondary=self._secondary_name,
                reason=str(exc)[:120],
            )
            raise ConnectionError(
                f"primary ({self.name}) and backup gateway both unreachable"
            ) from exc
        if _is_error_relay(result):
            # The backup answered with an error (measured 2026-10-25 and
            # again 2026-10-27 00:03Z: proxy_ms 429 "insufficient balance",
            # i.e. its own billing is dead). Two hard rules:
            #   1. do NOT relay the backup error verbatim — the client would
            #      read the backup's billing 429 as the *primary* chain's
            #      quota wall ("provider: oc1id ... billing"), classify it
            #      non-retryable and stop, and ops would chase IP rotation
            #      that cannot fix an account balance. Reshape it into one
            #      gateway-owned retryable 503 instead.
            #   2. do NOT keep parking traffic on a backup that cannot
            #      serve: release the breaker so the next request probes
            #      the primary again (the primary 429 may not have been a
            #      real quota burn at all — the FreeUsageLimitError shape is
            #      reused by the transport, prompt and session gates).
            status = int(result.get("__status__") or 0)
            logger.error(
                "PRODUCTION INCIDENT - FALLBACK FAILED: backup gateway %s "
                "answered HTTP %s; releasing the hold so the next request "
                "retries the primary (primary fault was: %s)",
                self._secondary_name,
                status,
                reason,
            )
            EVENTS.emit(
                "fallback_failed",
                severity="incident",
                primary=self.name,
                secondary=self._secondary_name,
                status=status,
                reason=reason[:120],
            )
            self._fallback_until = 0.0
            return _backup_error_relay(status, self._secondary_name, reason)
        return result

    def _announce_recovery(self) -> None:
        self._last_trip_at = 0.0
        self._primary_streak = 0
        logger.warning(
            "PRODUCTION INCIDENT CLOSED - FALLBACK RECOVERED: primary upstream "
            "%s is serving again; traffic returned to the primary chain",
            self.name,
        )
        EVENTS.emit(
            "fallback_recovered",
            severity="recovered",
            primary=self.name,
            secondary=self._secondary_name,
        )

    async def aclose(self) -> None:
        for adapter in (self._primary, self._secondary):
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()


def _describe(exc: BaseException) -> str:
    """One-line, content-free fault description for logs/events."""

    detail = str(exc).strip().replace("\n", " ")[:120]
    name = type(exc).__name__
    if name in ("TimeoutError", "Timeout"):
        return f"primary timeout: {detail}" if detail else "primary timeout"
    return f"{name}: {detail}" if detail else name
