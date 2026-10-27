"""Tests for egress_agent.logging_setup."""

from __future__ import annotations

import json
import logging


def test_json_formatter_emits_one_line(caplog) -> None:
    from egress_agent.logging_setup import JsonFormatter

    record = logging.LogRecord(
        name="egress_agent", level=logging.INFO, pathname=__file__, lineno=1,
        msg="tunnel opened %s", args=("wifi",), exc_info=None,
    )
    record.egress = "owin10-mobile"
    line = JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["msg"] == "tunnel opened wifi"
    assert payload["egress"] == "owin10-mobile"
    assert "ts" in payload and "logger" in payload


def test_setup_logging_is_idempotent_and_applies_level() -> None:
    from egress_agent.logging_setup import setup_logging

    setup_logging("WARNING")
    setup_logging("WARNING")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.WARNING
