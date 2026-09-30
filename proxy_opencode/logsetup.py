"""Logging setup: human-readable text by default, JSON lines via LOG_FORMAT=json.

Red line (see AGENTS.md): never log message content, prompts, or any key.
Only the whitelisted FIELDS below are ever rendered, so an accidental
extra can't leak sensitive data into logs.
"""

from __future__ import annotations

import json
import logging
import os
import time

# Whitelisted structured fields. Everything else in record.__dict__ is
# ignored by both formatters. Adding a field here is the ONLY way an `extra`
# reaches the logs, so keep it in sync with the call sites: a name that is
# logged but missing here is silently dropped (and has bitten us before —
# key_id/ttl_hours/path used to vanish).
FIELDS = (
    "request_id",
    "model",
    "stream",
    "has_tools",
    "status",
    "latency_ms",
    "usage",
    "client",
    "reason",
    "provided",
    "mode",
    "bind",
    "keys",
    "admin_keys",
    "public_models",
    "adapter",
    "version",
    "chunks",
    "ttfb_ms",
    "duration_ms",
    "error_detail",
    "key_id",
    "key_name",
    "ttl_hours",
    "path",
    "shape",
)

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class ExtraFormatter(logging.Formatter):
    """Text formatter that appends whitelisted extras as key=value pairs."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(
            f"{name}={record.__dict__[name]}" for name in FIELDS if name in record.__dict__
        )
        return f"{base} {extras}" if extras else base


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts/level/logger/msg + whitelisted extras."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in FIELDS:
            if name in record.__dict__:
                out[name] = record.__dict__[name]
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """Configure root logging. LOG_FORMAT=json switches to JSON lines."""
    log_format = os.environ.get("LOG_FORMAT", "text").strip().lower()
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            ExtraFormatter(fmt=_TEXT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
        )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
