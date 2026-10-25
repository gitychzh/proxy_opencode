"""Backup-upstream failover wrapper: zen-direct primary -> proxy_ms secondary.

Owner requirement (2026-10-25): when the zen free-tier chain fails at the
egress level (quota 429, 5xx, transport failure, free-tier gate 503), the
gateway must switch to the ModelScope aggregation gateway (``proxy_ms``,
same ECS host, :10003) automatically and conspicuously — then come back.

Design:
  * Wrapper adapter around the primary (built in ``build_adapter``). All
    three protocol routes plus /v1/models consume ``app.state.adapter``, so
    one wrapper covers every surface.
  * Failover triggers are EGRESS-level only: a raised ConnectionError or an
    error-relay dict whose ``__status__`` is 429/5xx/503. Request-level 4xx
    (bad model id, bad key) is the client's fault — relayed untouched.
  * Circuit breaker: once tripped, every request goes straight to the
    secondary for ``ZEN_FALLBACK_HOLD_S`` (default 120s); the first request
    after the window probes the primary again (half-open). A primary
    success closes the breaker and logs the recovery.
  * Prominent notifications (red-line compliant: no content, no keys):
      - WARNING "FALLBACK ENGAGED" / "FALLBACK RECOVERED" log records
      - structured JSONL events via the shared egress event sink
      - /healthz grows a "fallback" block (engaged / hold remaining)
  * Model ids: the secondary speaks ModelScope ids, not zen bare ids.
    ``ZEN_FALLBACK_MODEL_MAP`` (``from=to`` csv) rewrites the upstream model
    before handing the payload over; unmapped ids pass through unchanged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from ..config import Settings
from ..egress.eventlog import EVENTS
from .openai_http import OpenAIHttpAdapter

logger = logging.getLogger("proxy_opencode.failover")

# Egress-level upstream statuses worth a backup attempt. 429 = free-tier
# quota (per-IP/UTC-day); 5xx = upstream/transport; 503 = the zen adapter's
# own egress-gate answer (cordoned pool / free-tier 403 rewrite). Anything
# else is request-level and relayed verbatim.
_FAILOVER_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_error_relay(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("__status__"), int)


class FailoverAdapter:
    """Serve chat through ``primary``; fail over to proxy_ms on egress faults."""

    def __init__(
        self,
        primary: Any,
        settings: Settings,
        secondary: Any | None = None,
    ) -> None:
        self._primary = primary
        self.name = primary.name
        self._model_map = dict(settings.zen_fallback_model_map)
        self._hold_s = max(1.0, float(settings.zen_fallback_hold_s))
        # Secondary client: the plain OpenAI passthrough adapter pointed at
        # the backup gateway (proxy_ms), reusing its stream relay semantics.
        # Tests inject a pre-built secondary instead (see inject_secondary).
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

    # ------------------------------------------------------------ status

    def fallback_status(self) -> dict[str, Any]:
        """/healthz block: is the backup chain currently serving?"""
        remaining = self._fallback_until - time.monotonic()
        return {
            "engaged": remaining > 0,
            "secondary": self._secondary_name,
            "hold_remaining_s": round(remaining, 1) if remaining > 0 else 0,
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
        if time.monotonic() < self._fallback_until:
            # Breaker open: skip the (recently failed) primary entirely.
            return await self._via_secondary(payload, "hold window")
        try:
            result = await self._primary.chat(payload)
        except ConnectionError as exc:
            return await self._trip(payload, f"transport failure: {exc}")
        if _is_error_relay(result) and int(result["__status__"]) in _FAILOVER_STATUSES:
            return await self._trip(
                payload, f"upstream HTTP {result['__status__']}"
            )
        if self._fallback_until:
            # Half-open probe succeeded: close the breaker, announce it.
            self._fallback_until = 0.0
            self._announce_recovery()
        return result

    # ---------------------------------------------------------- internals

    async def _trip(self, payload: dict[str, Any], reason: str) -> Any:
        self._fallback_until = time.monotonic() + self._hold_s
        logger.warning(
            "FALLBACK ENGAGED: primary upstream %s unavailable (%s); "
            "serving via backup gateway for the next %.0fs",
            self.name,
            reason,
            self._hold_s,
        )
        EVENTS.emit(
            "fallback_engaged",
            primary=self.name,
            reason=reason[:120],
            hold_s=self._hold_s,
            secondary=self._secondary_name,
        )
        return await self._via_secondary(payload, reason)

    async def _via_secondary(self, payload: dict[str, Any], reason: str) -> Any:
        body = dict(payload)
        mapped = self._model_map.get(str(body.get("model") or ""))
        if mapped:
            body["model"] = mapped
        try:
            return await self._secondary.chat(body)
        except ConnectionError as exc:
            # Backup also unreachable: surface it loudly, nothing else to try.
            logger.error(
                "FALLBACK FAILED: backup gateway %s unreachable (%s); "
                "primary was down (%s)",
                self._secondary_name,
                exc,
                reason,
            )
            EVENTS.emit(
                "fallback_failed",
                primary=self.name,
                secondary=self._secondary_name,
                reason=str(exc)[:120],
            )
            raise ConnectionError(
                f"primary ({self.name}) and backup gateway both unreachable"
            ) from exc

    def _announce_recovery(self) -> None:
        logger.warning(
            "FALLBACK RECOVERED: primary upstream %s is serving again; "
            "traffic returned to the primary chain",
            self.name,
        )
        EVENTS.emit(
            "fallback_recovered",
            primary=self.name,
            secondary=self._secondary_name,
        )

    async def aclose(self) -> None:
        for adapter in (self._primary, self._secondary):
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()
