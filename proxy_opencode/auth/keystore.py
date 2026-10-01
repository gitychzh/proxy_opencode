"""Dynamic API key store with expiry, persisted as a JSON file.

Static keys (GATEWAY_API_KEYS / ADMIN_API_KEYS env) live in Settings and are
permanent. Keys minted via POST /admin/keys live here: they default to 24h
validity and stop authenticating the moment they expire or are revoked.

SCOPE NOTE: the store is per-process (in-memory list + whole-file rewrite).
Run the gateway with a single worker (the default `python -m proxy_opencode`
does exactly that); with `--workers N` each worker keeps its own copy of
`keys.json` and minted/revoked keys would not propagate between workers.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("proxy_opencode.auth.keys")

_SCHEMA_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _matches(stored: str, presented: str) -> bool:
    """Constant-time key comparison (non-ASCII input never matches)."""
    try:
        return hmac.compare_digest(stored, presented)
    except TypeError:
        return False


@dataclass
class KeyRecord:
    id: str
    key: str
    name: str
    created_at: str
    expires_at: str | None = None  # None = permanent
    revoked: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def expiry(self) -> datetime | None:
        return _parse_iso(self.expires_at)

    def is_valid(self, now: datetime | None = None) -> bool:
        if self.revoked:
            return False
        exp = self.expiry()
        if exp is not None and (now or _utcnow()) >= exp:
            return False
        return True

    def public_view(self) -> dict[str, Any]:
        """Masked form for admin listings (never exposes the full key)."""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "key_preview": self.key[:10] + "…" + self.key[-4:]
            if len(self.key) > 16
            else self.key[:4] + "…",
        }


class KeyStore:
    def __init__(self, path: str | Path, default_ttl_hours: float = 24.0) -> None:
        self.path = Path(path)
        self.default_ttl_hours = default_ttl_hours
        self._records: list[KeyRecord] = []
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning(
                "key store unreadable; starting empty",
                extra={"path": str(self.path)},
            )
            return
        if not isinstance(data, dict):
            # A corrupted/foreign file (e.g. a JSON array) must not crash
            # startup: refuse it loudly and start from an empty store instead
            # of clobbering it — _save() only runs on the next mutation.
            logger.warning(
                "key store has unexpected top-level shape; starting empty",
                extra={"path": str(self.path), "shape": type(data).__name__},
            )
            return
        keys = data.get("keys")
        if not isinstance(keys, list):
            return
        for item in keys:
            if not isinstance(item, dict):
                continue
            expires_at = item.get("expires_at")
            meta = item.get("meta")
            self._records.append(
                KeyRecord(
                    id=str(item.get("id") or ""),
                    key=str(item.get("key") or ""),
                    name=str(item.get("name") or ""),
                    created_at=str(item.get("created_at") or ""),
                    expires_at=expires_at if isinstance(expires_at, str) else None,
                    revoked=bool(item.get("revoked")),
                    meta=meta if isinstance(meta, dict) else {},
                )
            )

    def _save(self) -> None:
        data = {
            "version": _SCHEMA_VERSION,
            "keys": [
                {
                    "id": r.id,
                    "key": r.key,
                    "name": r.name,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "revoked": r.revoked,
                    "meta": r.meta,
                }
                for r in self._records
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a crashed write must never corrupt the store.
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".keys-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------- operations

    def create(
        self,
        name: str = "",
        ttl_hours: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> KeyRecord:
        """Mint a key. ttl_hours=None -> store default (24h); 0 -> permanent."""
        # Defence in depth: NaN compares False on every bound, so letting it
        # reach the `> 0` branch below would mint a *permanent* key. A
        # non-finite ttl falls back to the store default instead of ever
        # meaning "permanent".
        if ttl_hours is None or not math.isfinite(ttl_hours):
            effective_ttl = self.default_ttl_hours
        else:
            effective_ttl = ttl_hours
        now = _utcnow()
        expires_at: str | None = None
        if effective_ttl is not None and effective_ttl > 0:
            expires_at = _iso(now + timedelta(hours=effective_ttl))
        record = KeyRecord(
            id="k-" + secrets.token_hex(4),
            key="gw-" + secrets.token_urlsafe(24),
            name=name,
            created_at=_iso(now),
            expires_at=expires_at,
            meta=meta or {},
        )
        self._records.append(record)
        self._save()
        logger.info(
            "api key created",
            extra={"key_id": record.id, "ttl_hours": effective_ttl, "key_name": name},
        )
        return record

    def validate(self, key: str) -> KeyRecord | None:
        for record in self._records:
            if _matches(record.key, key) and record.is_valid():
                return record
        return None

    def lookup_expired(self, key: str) -> KeyRecord | None:
        """Same as validate() but also matches expired/revoked entries."""
        for record in self._records:
            if _matches(record.key, key):
                return record
        return None

    def list(self) -> list[KeyRecord]:
        return list(self._records)

    def revoke(self, key_id: str) -> bool:
        for record in self._records:
            if record.id == key_id and not record.revoked:
                record.revoked = True
                self._save()
                logger.info("api key revoked", extra={"key_id": key_id})
                return True
        return False
