#!/usr/bin/env python3
"""Collect ECS-side evidence for one DSH triad run and join it to the cases.

For every case in the runner's report (which carries exact UTC start/end
windows) this collects three independent sources from the ECS host:

* zen-gw structured ``chat completion`` log lines (route / fallback / model),
* the /data JSONL egress event ledger (per upstream attempt: bucket, IP, ok,
  kind, cordon, rotation),
* the admin/egresses snapshot (per-bucket counters incl. token budget).

It then writes ``joined.jsonl`` (one row per case with its gateway calls and
upstream attempts) and ``analysis_summary.json``. Raw sources are kept so a
later, deeper analysis does not depend on this script's interpretation.

Usage:
    python3 scripts/collect_triad_evidence.py --report <triad report.json> \
        --out <evidence dir>
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SSH = ["ssh", "-p", "222", "-o", "BatchMode=yes", "root@115.29.231.25"]
ECS_DIR = "/opt/zen-gw/src/deploy/ecs"


def _ssh(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return subprocess.check_output(
        SSH + [f"echo {encoded} | base64 -d | bash"], text=True
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _gw_records(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        body = line.split("| ", 1)[-1].strip()
        if not body.startswith("{"):
            continue
        try:
            rec = json.loads(body)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("msg"):
            out.append(rec)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    start = report["window_start_utc"]
    end = report["window_end_utc"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    logs_raw = _ssh(
        f"cd {ECS_DIR} && docker compose logs --no-color "
        f"--since '{start}' zen-gw 2>&1"
    )
    (out / "gw_logs_raw.txt").write_text(logs_raw, encoding="utf-8")

    events_raw = _ssh(
        f"cd {ECS_DIR}\n"
        "python3 - <<'PY'\n"
        "import json\n"
        f'start, end = "{start}", "{end}"\n'
        "for line in open('data/egress_events.jsonl', encoding='utf-8'):\n"
        "    try:\n"
        "        row = json.loads(line)\n"
        "    except ValueError:\n"
        "        continue\n"
        '    if start <= row.get("ts", "") <= end:\n'
        "        print(json.dumps(row, ensure_ascii=False))\n"
        "PY\n"
    )
    (out / "egress_events_window.jsonl").write_text(
        events_raw, encoding="utf-8"
    )

    admin_raw = _ssh(
        f"cd {ECS_DIR} && ADM=$(grep '^ADMIN_API_KEYS=' .env | cut -d= -f2- "
        "| cut -d, -f1) && "
        'curl -sS -m 10 -H "Authorization: Bearer $ADM" '
        "http://127.0.0.1:10002/admin/egresses"
    )
    (out / "after_admin.json").write_text(admin_raw, encoding="utf-8")

    gw = _gw_records(logs_raw)
    events = [
        json.loads(line)
        for line in events_raw.splitlines()
        if line.strip().startswith("{")
    ]
    chats = [r for r in gw if r.get("msg") == "chat completion"]

    joined: list[dict[str, Any]] = []
    for case in report["results"]:
        lo, hi = _parse_ts(case["started_utc"]), _parse_ts(case["ended_utc"])
        matched = []
        for rec in chats:
            ts_raw = str(rec.get("ts", "")).replace("Z", "+00:00")
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if not ts.tzinfo:
                ts = ts.replace(tzinfo=timezone.utc)
            if lo <= ts <= hi and rec.get("model") == case["model"]:
                matched.append(rec)
        attempts = []
        for ev in events:
            if ev.get("event") != "egress_request":
                continue
            try:
                ts = datetime.fromisoformat(
                    str(ev.get("ts", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if not ts.tzinfo:
                ts = ts.replace(tzinfo=timezone.utc)
            if lo <= ts <= hi:
                attempts.append(ev)
        routes = [
            "fallback" if r.get("fallback") else "primary" for r in matched
        ]
        if not matched:
            route = "unmatched"
        elif all(r == "primary" for r in routes):
            route = "primary"
        elif all(r == "fallback" for r in routes):
            route = "fallback"
        else:
            route = "mixed"
        row = {**case, "route": route, "gw_calls": matched, "attempts": attempts}
        joined.append(row)

    (out / "joined.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in joined) + "\n",
        encoding="utf-8",
    )

    kinds = Counter(
        str(ev.get("kind")) for ev in events if ev.get("event") == "egress_request"
    )
    names = Counter(
        str(ev.get("egress")) for ev in events if ev.get("event") == "egress_request"
    )
    event_names = Counter(str(ev.get("event")) for ev in events)
    summary = {
        "run_id": report.get("run_id"),
        "window": [start, end],
        "cases": len(joined),
        "runner_passed": report.get("passed"),
        "routes": dict(Counter(r["route"] for r in joined)),
        "by_suite": dict(Counter(r["suite"] for r in joined)),
        "by_model": dict(Counter(r["model"] for r in joined)),
        "by_model_route": dict(
            Counter(f"{r['model']}|{r['route']}" for r in joined)
        ),
        "gw_chat_lines": len(chats),
        "gw_all_lines": len(gw),
        "event_kinds_in_window": dict(event_names),
        "egress_request_kinds": dict(kinds),
        "egress_request_buckets": dict(names),
        "cases_with_429": [
            r["case_id"]
            for r in joined
            if any("429" in str(a.get("kind")) for a in r["attempts"])
        ],
        "cases_without_gw_log": [
            r["case_id"] for r in joined if r["route"] == "unmatched"
        ],
    }
    (out / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
