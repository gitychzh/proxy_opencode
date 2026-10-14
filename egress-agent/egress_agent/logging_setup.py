"""Structured logging that never carries payload content.

The gateway relays prompts and completions through the agent's proxy; the
agent must be able to debug connectivity without ever seeing them. All log
records are single-line JSON so they pipe into journald/Loki unchanged.

Loggable facts: connection lifecycle counts, byte counts, egress IP probes,
ONT rotation steps. Never log: URL paths, Host headers, CONNECT targets,
request/response bodies.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``extra`` fields are merged at top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Install the JSON formatter on the root logger (idempotent)."""

    handler = logging.StreamHandler(stream=sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
