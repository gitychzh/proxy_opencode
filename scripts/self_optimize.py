#!/usr/bin/env python3
"""Self-optimization report: read the gateway's own telemetry, emit findings.

The analysis lives in ``proxy_opencode/insights.py`` (unit-tested, covered by
the ruff/mypy gate); this file is only the CLI: read files -> analyze ->
print or write a report.

Usage examples:

    # on the ECS host (inside the container image or next to the repo)
    python scripts/self_optimize.py \\
        --events /data/egress_events.jsonl \\
        --log-cmd "docker logs zen-gw --since 24h 2>&1"

    # from a workstation, pulling both sources over ssh first
    ssh -p 222 root@115.29.231.25 \\
        'tail -c 2000000 /data/egress_events.jsonl' > /tmp/events.jsonl
    ssh -p 222 root@115.29.231.25 \\
        'docker logs zen-gw --since 24h 2>&1' > /tmp/gw.log
    python scripts/self_optimize.py --events /tmp/events.jsonl --log /tmp/gw.log

Exit codes: 0 = no critical finding, 1 = at least one critical finding,
2 = usage/IO error. The loop in docs/SELF_OPTIMIZATION.md keys off that.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from proxy_opencode.insights import (  # noqa: E402
    EVENTS_WINDOW_HOURS,
    analyze,
    render_markdown,
)

EXIT_CRITICAL = 1
EXIT_USAGE = 2


def _read(path: str) -> str:
    """Read a file, or stdin for ``-``; a missing file is empty, not fatal."""

    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _run(cmd: str) -> str:
    """Run a log-dump command and return stdout (empty on failure)."""

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warn: log command failed: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        print(
            f"warn: log command exited {proc.returncode}: "
            f"{proc.stderr.strip()[:200]}",
            file=sys.stderr,
        )
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn gateway telemetry into deterministic optimization findings."
    )
    parser.add_argument(
        "--events",
        default="",
        help="JSONL egress event ledger path ('-' = stdin; empty = skip).",
    )
    parser.add_argument(
        "--log", default="", help="gateway log path ('-' = stdin; empty = skip)."
    )
    parser.add_argument(
        "--log-cmd",
        default="",
        help="shell command whose stdout is used as the gateway log "
        "(e.g. 'docker logs zen-gw --since 24h 2>&1').",
    )
    parser.add_argument(
        "--ttfb-s",
        type=float,
        default=60.0,
        help="live ZEN_TTFB_S value, used to judge the header deadline (default 60).",
    )
    parser.add_argument(
        "--fallback-model",
        default="",
        help="expected proxy_ms model (e.g. ZhipuAI/GLM-5.3-Flash) for drift checks.",
    )
    parser.add_argument(
        "--events-window-hours",
        type=float,
        default=EVENTS_WINDOW_HOURS,
        help="cut ledger events to this many hours back from the newest "
        "timestamp on either side, so the events side matches a log dumped "
        f"with '--since <n>h' (default {EVENTS_WINDOW_HOURS:g}; 0 = keep all).",
    )
    parser.add_argument(
        "--out", default="", help="write the markdown report here instead of stdout."
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the raw summary+findings JSON."
    )
    args = parser.parse_args(argv)

    if not (args.events or args.log or args.log_cmd):
        parser.error("at least one of --events / --log / --log-cmd is required")

    event_text = _read(args.events) if args.events else ""
    log_text = _read(args.log) if args.log else ""
    if args.log_cmd:
        log_text += _run(args.log_cmd)

    summary, findings = analyze(
        event_text,
        log_text,
        ttfb_deadline_s=args.ttfb_s,
        fallback_model_hint=args.fallback_model,
        events_window_hours=args.events_window_hours,
    )

    if args.json:
        payload = {"summary": summary, "findings": findings}
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        rendered = render_markdown(summary, findings)

    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"report written: {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)

    critical = [f for f in findings if f.get("severity") == "critical"]
    return EXIT_CRITICAL if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
