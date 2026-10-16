"""Zen free-model catalog: discovery, TTL cache and passthrough seeding.

The Zen catalogue (``GET {zen_base}/models``) mixes paid and free models.
The anonymous free tier can only ever *use* the free ones, and the user
experience contract for this gateway is:

* the public alias (``deepseek-ai/deepseek-v4.1-flash`` -> ``big-pickle``)
  stays masked in the standard ``/v1/models`` response — scanners of that
  standard path must not enumerate the real upstream ids;
* every OTHER free model passes through verbatim: the full free-id list
  is served ONLY by the hidden ``/v1/models_cus`` endpoint (the ``_cus``
  is the contract — do not add stray variants of the path, the point is
  that model-scanning agents probing standard paths find nothing).

Free-model detection: upstream entries may carry explicit markers
(``allowAnonymous`` / ``cost`` / ``pricing``); when they are absent
(the anonymous view returns bare ids), the stable upstream convention
``id ends with "-free"`` plus the explicit ``big-pickle`` decides.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .eventlog import EVENTS

logger = logging.getLogger("proxy_opencode.egress.catalog")

FREE_SUFFIX = "-free"
# The original anonymous-tier workhorse carries no suffix at all.
EXPLICIT_FREE_IDS = frozenset({"big-pickle"})
_REFRESH_TTL_S = 600.0
_REFRESH_ERROR_BACKOFF_S = 300.0


def is_free_entry(entry: dict[str, Any]) -> bool:
    """Decide whether one upstream catalogue entry is anonymous-usable."""

    mid = entry.get("id")
    if not isinstance(mid, str) or not mid:
        return False
    # Explicit markers win when upstream provides them (authed views may).
    if entry.get("allowAnonymous") is True:
        return True
    cost = entry.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost) == 0.0
    pricing = entry.get("pricing")
    if isinstance(pricing, dict) and pricing:
        try:
            return all(float(v) == 0.0 for v in pricing.values())
        except (TypeError, ValueError):
            return False
    # Anonymous view: bare ids, no pricing fields — upstream's own naming
    # convention is the marker (live 2026-10-16: cat-jam-free,
    # space-bunny-free, deepseek-v4.1-flash-free, ...).
    return mid.endswith(FREE_SUFFIX) or mid in EXPLICIT_FREE_IDS


class ZenFreeCatalog:
    """TTL-cached fetcher of the Zen free-model ids (ECS-host side)."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        ttl_s: float = _REFRESH_TTL_S,
        registry: Any = None,
        fallback_ids: list[str] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        parsed = httpx.URL(base_url)
        if (parsed.host or "") not in ("opencode.ai", "localhost", "127.0.0.1"):
            raise ValueError(
                f"ZenFreeCatalog host not allowed (got {base_url!r}); "
                "mirror the zen-direct base-url constraint"
            )
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._ttl_s = max(60.0, float(ttl_s))
        self._registry = registry
        self._timeout_s = timeout_s
        self._fallback = [
            i
            for i in (fallback_ids or [])
            if isinstance(i, str) and i.strip()
        ]
        self._ids: list[str] = []
        self._degraded: bool | None = None  # None = never refreshed yet
        self._refreshed_at: float = 0.0
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # Seed the passthrough set so a cold start before the first fetch
        # still resolves the static default (big-pickle) verbatim.
        seed = {i for i in self._fallback if is_free_entry({"id": i})}
        if self._registry is not None and seed:
            self._registry.set_free_ids(seed)
            self._ids = sorted(seed)

    # ----------------------------------------------------------------- API

    async def refresh(self) -> bool:
        """Fetch the upstream catalogue once; True on success."""

        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(self._timeout_s, connect=5.0),
            ) as client:
                resp = await client.get(f"{self._base}/models", headers=headers)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if self._degraded is None and self._fallback:
                logger.warning(
                    "zen models unreachable; serving static fallback list",
                    extra={"error": type(exc).__name__},
                )
            self._degraded = True
            return False
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            self._degraded = True
            return False
        free = sorted(
            str(entry["id"])
            for entry in data
            if isinstance(entry, dict) and is_free_entry(entry)
        )
        self._ids = free
        self._degraded = False
        self._refreshed_at = datetime.now(tz=timezone.utc).timestamp()
        if self._registry is not None:
            self._registry.set_free_ids(free)
        logger.info(
            "zen free-model catalog refreshed",
            extra={"count": len(free), "model": "catalog"},
        )
        EVENTS.emit("free_catalog_refreshed", count=len(free), ids=free)
        return True

    def ids(self) -> list[str]:
        """Last-known free ids (static fallback when nothing worked)."""

        if self._ids:
            return list(self._ids)
        return [i for i in self._fallback if is_free_entry({"id": i})]

    @property
    def degraded(self) -> bool | None:
        """None = never refreshed; True = upstream unreachable; False = ok."""

        return self._degraded

    @property
    def refreshed_at(self) -> str | None:
        if not self._refreshed_at:
            return None
        return datetime.fromtimestamp(
            self._refreshed_at, tz=timezone.utc
        ).isoformat(timespec="seconds")

    def body(self) -> dict[str, Any]:
        """OpenAI-shaped list body for the hidden /v1/models_cus endpoint."""

        ids = self.ids()
        return {
            "object": "list",
            "data": [
                {"id": i, "object": "model", "created": 0, "owned_by": "opencode"}
                for i in ids
            ],
            "metadata": {
                "source": "opencode-zen free models",
                "refreshed_at": self.refreshed_at,
                "stale": self._degraded is True,
            },
        }

    # ------------------------------------------------------------ lifecycle

    async def _loop(self) -> None:
        await self.refresh()
        delay = self._ttl_s
        while not self._closed:
            await asyncio.sleep(delay)
            ok = await self.refresh()
            # A failed fetch retries sooner than a healthy TTL pass.
            delay = self._ttl_s if ok else _REFRESH_ERROR_BACKOFF_S

    async def start(self) -> None:
        self._closed = False
        self._task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
