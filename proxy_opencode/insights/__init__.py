"""Log-driven self-optimization: turn the gateway's own telemetry into findings.

Inputs are the three observation surfaces the gateway already produces, and all
three are red-line clean (no prompts, no message content, no keys):

* the JSONL egress event ledger (``ZEN_EGRESS_EVENT_LOG``) — cordons,
  rotations, fallback engaged/failed/recovered, window rollovers, IP changes;
* request / stream log lines (text or ``LOG_FORMAT=json``) — the
  ``chat completion`` record (status, latency_ms, usage, fallback provenance)
  and the ``stream ended`` record (chunks, ttfb_ms, duration_ms, reason);
* optionally a ``/admin/egresses`` snapshot.

Everything here is a pure function over parsed records, so the loop's
conclusions are deterministic and unit-testable instead of relying on a human
reading dashboards: same input -> same findings, every run.

Modules:

``parse``     JSONL ledger + text/JSON log lines
``metrics``   counters, ratios, nearest-rank percentiles, phase split
``diagnose``  the rule set (thresholds live there)
``render``    Markdown report

The CLI wrapper is ``scripts/self_optimize.py``; the loop that consumes its
output is documented in ``docs/SELF_OPTIMIZATION.md``.
"""

from __future__ import annotations

from typing import Any

from .diagnose import diagnose
from .metrics import EVENTS_WINDOW_HOURS, percentile, summarize, window_events
from .parse import parse_event_records, parse_log_lines, record_epoch
from .render import render_markdown


def analyze(
    event_text: str,
    log_text: str,
    *,
    ttfb_deadline_s: float = 60.0,
    fallback_model_hint: str = "",
    events_window_hours: float = EVENTS_WINDOW_HOURS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One-shot pipeline: raw text in, (summary, findings) out.

    ``events_window_hours`` cuts the ledger to the log's window; pass 0 to
    count every ledger record (only meaningful when both sides were already
    dumped over the same period).
    """

    summary = summarize(
        parse_event_records(event_text),
        parse_log_lines(log_text),
        window_hours=events_window_hours,
    )
    findings = diagnose(
        summary,
        ttfb_deadline_s=ttfb_deadline_s,
        fallback_model_hint=fallback_model_hint,
    )
    return summary, findings


__all__ = [
    "EVENTS_WINDOW_HOURS",
    "analyze",
    "diagnose",
    "parse_event_records",
    "parse_log_lines",
    "percentile",
    "record_epoch",
    "render_markdown",
    "summarize",
    "window_events",
]
