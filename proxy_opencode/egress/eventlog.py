"""Structured egress event log: one JSON object per line on the ECS host.

The 0.7.x gateway (and therefore every decision worth observing — bucket
cordons, quota 429s, ONT rotations with the before/after IP comparison,
probe-observed IP changes) runs inside one Docker container on the ECS
host. This module appends those events to a JSONL file on the ``/data``
volume so an operator can answer "why did bucket X rotate at 14:32, what
was the IP before and after?" without parsing docker logs.

Rotation is cheap size-based (bytes), keeping a couple of rotated files.
Red line: events carry egress names, IPs, counts, durations and
timestamps only — never message content, prompts or credentials (source
code never passes such values here; see AGENTS.md).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

_MAX_BYTES = 5 * 1024 * 1024
_KEEP_ROTATED = 2


class EventLog:
    """Size-rotating JSONL writer; silently disabled when unusable."""

    def __init__(self) -> None:
        self._path = ""
        self._lock = threading.Lock()
        self._disabled = False

    @property
    def path(self) -> str:
        return self._path

    def configure(self, path: str) -> None:
        """Point the sink at ``path`` ("" disables). One-time setup wire."""

        self._path = (path or "").strip()
        self._disabled = False

    def emit(self, event: str, **fields: Any) -> None:
        """Append one event; I/O failures disable the sink (never raise)."""

        if not self._path or self._disabled:
            return
        record: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                self._rotate_if_needed()
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError as exc:
                self._disabled = True
                logging.getLogger("proxy_opencode.egress.eventlog").error(
                    "egress event log disabled after write failure",
                    extra={"error": type(exc).__name__,
                           "error_detail": str(exc)[:120]},
                )

    # -------------------------------------------------------------- rotation

    def _rotate_if_needed(self) -> None:
        path = self._path
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < _MAX_BYTES:
            return
        # Deterministic shift: path.N <- path.(N-1) ... path.1 <- path.
        # (timestamp-named rotations can collide within the same second.)
        for i in range(_KEEP_ROTATED, 0, -1):
            src = path if i == 1 else f"{path}.{i - 1}"
            dst = f"{path}.{i}"
            try:
                os.replace(src, dst)
            except OSError:
                continue


EVENTS = EventLog()
"""Module singleton; the app factory configures it from settings and every
egress component emits through it. Tests reconfigure/reload per case."""
