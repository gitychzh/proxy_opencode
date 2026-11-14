"""Daily snapshot of the opencode Zen free-model ids.

The gateway exposes these ids verbatim (passthrough) on top of the legacy
public alias. A host-side job refreshes the snapshot once a day; the gateway
hot-reloads it by mtime, so a catalogue change needs no container restart and
an unchanged catalogue causes no write at all.

Single source of truth for two consumers:

* ``ModelRegistry`` (gateway process) reads the snapshot;
* ``scripts/sync_zen_free_models.py`` (ECS host, systemd timer) writes it.

Only the standard library is used: the sync job runs on the ECS host with the
system python, outside the container where httpx and friends are installed.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
# The anonymous catalogue is public; this bearer value is a protocol literal,
# not a credential (AGENTS.md section 1, model gate). Never log it.
ANONYMOUS_BEARER = "public"
ENV_KEY = "OPENCODE_ZEN_MODELS"
FREE_SUFFIX = "-free"
EXPLICIT_FREE_IDS = frozenset({"big-pickle"})

Fetcher = Callable[[str, dict[str, str], float], bytes]


def is_free_entry(entry: dict[str, Any]) -> bool:
    """Decide whether one upstream catalogue entry is anonymous-usable.

    Explicit markers win when upstream provides them. The live anonymous
    ``/models`` view carries a nested ``cost`` object (paid entries are also
    zero there), so the naming convention is the reliable signal: ids ending
    in ``-free`` plus the original anonymous workhorse ``big-pickle``.
    """

    mid = entry.get("id")
    if not isinstance(mid, str) or not mid:
        return False
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
    return mid.endswith(FREE_SUFFIX) or mid in EXPLICIT_FREE_IDS


def _http_get(url: str, headers: dict[str, str], timeout_s: float) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _clean_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({str(v).strip() for v in values if str(v).strip()})


def fetch_free_ids(
    base_url: str = DEFAULT_BASE_URL,
    *,
    api_key: str = "",
    timeout_s: float = 15.0,
    fetch: Fetcher | None = None,
) -> list[str]:
    """Anonymous GET ``<base_url>/models`` -> sorted free bare ids."""

    url = f"{base_url.rstrip('/')}/models"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key or ANONYMOUS_BEARER}",
    }
    raw = (fetch or _http_get)(url, headers, timeout_s)
    body = json.loads(raw.decode("utf-8"))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise ValueError("upstream /models response has no data list")
    return _clean_ids(
        [
            str(entry.get("id") or "")
            for entry in data
            if isinstance(entry, dict) and is_free_entry(entry)
        ]
    )


class FreeModelsSnapshot:
    """mtime-driven hot reload of the free-model id list.

    A missing or corrupt file is an empty catalogue (the legacy alias still
    works). Reads take a lock so a request racing the daily rewrite never sees
    a half-written list.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._mtime_ns: int | None = None
        self._ids: list[str] = []
        self._updated_at = ""
        self._reload()

    @property
    def path(self) -> Path:
        return self._path

    def _stat_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _reload(self) -> None:
        stamp = self._stat_ns()
        ids: list[str] = []
        updated = ""
        if stamp is not None:
            try:
                body = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(body, dict):
                    ids = _clean_ids(body.get("ids"))
                    updated = str(body.get("updated_at") or "")
            except (OSError, ValueError, TypeError):
                ids, updated = [], ""
        self._ids, self._updated_at, self._mtime_ns = ids, updated, stamp

    def _current(self) -> None:
        if self._stat_ns() != self._mtime_ns:
            self._reload()

    def ids(self) -> list[str]:
        with self._lock:
            self._current()
            return list(self._ids)

    @property
    def updated_at(self) -> str:
        with self._lock:
            self._current()
            return self._updated_at

    def __contains__(self, model_id: object) -> bool:
        return isinstance(model_id, str) and model_id in self.ids()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep an existing file's mode: .env is 0600 and holds credentials, while
    # a brand-new snapshot must be world-readable so the container's uid 10002
    # can mount /data and read it. mkstemp alone would give both 0600.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_snapshot(
    path: str | Path,
    ids: list[str],
    *,
    updated_at: str = "",
    source: str = "opencode-zen anonymous /models",
) -> dict[str, Any]:
    """Atomically replace the snapshot; returns the written body."""

    body: dict[str, Any] = {
        "ids": _clean_ids(ids),
        "updated_at": updated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
    }
    _atomic_write(Path(path), json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    return body


def read_snapshot_ids(path: str | Path) -> list[str]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _clean_ids(body.get("ids") if isinstance(body, dict) else None)


def update_env_key(path: str | Path, key: str, value: str) -> bool:
    """Set ``KEY=value`` in a .env file, preserving every other line.

    Returns False and writes nothing when the line already matches. Commented
    examples are never touched. This is what makes "no change -> no action"
    hold even for the persisted exit configuration.
    """

    target = Path(path)
    try:
        # newline="" keeps CRLF byte-for-byte; the default universal-newline
        # read would silently rewrite a Windows .env to LF on every sync.
        with target.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    except OSError:
        original = ""
    lines = original.splitlines(keepends=True)
    prefix = f"{key}="
    out: list[str] = []
    found = False
    changed = False
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#") and stripped.startswith(prefix):
            found = True
            if line.endswith("\r\n"):
                eol = "\r\n"
            elif line.endswith("\n"):
                eol = "\n"
            else:
                eol = ""
            replacement = f"{prefix}{value}{eol}"
            if replacement != line:
                changed = True
            out.append(replacement)
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += "\n"
        out.append(f"{prefix}{value}\n")
        changed = True
    if not changed:
        return False
    _atomic_write(target, "".join(out))
    return True


@dataclass(frozen=True)
class SyncResult:
    changed: bool
    ids: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    state_written: bool = False
    env_written: bool = False
    reason: str = "unchanged"


def sync_free_models(
    *,
    state_file: str | Path,
    env_file: str | Path | None = None,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    timeout_s: float = 15.0,
    dry_run: bool = False,
    fetch: Fetcher | None = None,
) -> SyncResult:
    """Refresh the free-model exit configuration, but only when it changed.

    ``changed`` is decided against the last snapshot, not against the .env
    file: an unchanged upstream catalogue means zero writes and zero restarts.
    An empty upstream result is refused so a transient bad response can never
    wipe the published catalogue.
    """

    ids = fetch_free_ids(
        base_url, api_key=api_key, timeout_s=timeout_s, fetch=fetch
    )
    if not ids:
        raise ValueError("upstream returned no free models; refusing to wipe state")
    state = Path(state_file)
    previous = read_snapshot_ids(state)
    added = sorted(set(ids) - set(previous))
    removed = sorted(set(previous) - set(ids))
    first_run = not state.exists()
    changed = bool(added or removed) or first_run
    if not changed:
        return SyncResult(changed=False, ids=ids, reason="unchanged")
    if dry_run:
        return SyncResult(True, ids, added, removed, reason="dry-run")
    write_snapshot(state, ids)
    env_written = False
    if env_file is not None:
        env_written = update_env_key(env_file, ENV_KEY, ",".join(ids))
    reason = "first-run" if first_run else "changed"
    return SyncResult(True, ids, added, removed, True, env_written, reason)
