"""Parsers for the two telemetry formats the gateway emits.

Both accept the *shipped* formats, not an idealised one:

* the JSONL egress event ledger (one JSON object per line, torn lines after a
  rotation are expected and skipped);
* gateway log lines in either ``LOG_FORMAT=json`` or the default text formatter
  (``<ts> <LEVEL> <logger> <msg> k=v k=v ...``), where the message text must be
  recovered because it is what classifies a line as a request record or a
  stream record.

Nothing here interprets the data — see :mod:`.metrics` for that.
"""

from __future__ import annotations

import json
import re
from typing import Any

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
# Text formatter head: "<date> <time> <LEVEL> <logger> <message><extras>".
_TEXT_HEAD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T][\d:.,]+ +[A-Z]+ +(\S+) +(.*)$"
)
# Where the trailing key=value run starts; everything before it is the message.
_KV_START_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_]*=")


def parse_event_records(text: str) -> list[dict[str, Any]]:
    """Parse JSONL ledger text into records (malformed lines are skipped).

    A corrupt line must never abort the report: the ledger is appended to by a
    rotating writer, so a torn final line after a rotation is expected.
    """

    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _coerce(token: str) -> Any:
    if token in ("null", "None", ""):
        return None
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def parse_log_lines(text: str) -> list[dict[str, Any]]:
    """Parse gateway log lines into dicts of key=value extras.

    Values containing spaces (``cordon_reason=free_quota_exhausted (per-IP
    daily quota)``) are truncated at the first space by construction; every
    consumer keys off the prefix, so that is harmless and keeps the parser a
    single regex instead of a quoting grammar.
    """

    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict):
                out.append(record)
                continue
        extras: dict[str, Any] = {}
        head = _TEXT_HEAD_RE.match(line)
        if head is not None:
            # The message text is what classifies a line as a request record or
            # a stream record, so it must survive parsing: without it the
            # default (text) log format yields unattributable key/value soup.
            extras["logger"] = head.group(1)
            extras["msg"] = _KV_START_RE.split(head.group(2), maxsplit=1)[0].strip()
        for key, value in _KV_RE.findall(line):
            extras[key] = _coerce(value)
        if extras:
            out.append(extras)
    return out


__all__ = ["parse_event_records", "parse_log_lines"]
