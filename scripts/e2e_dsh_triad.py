#!/usr/bin/env python3
"""DSH E2E triad: chat + tool use + reasoning across every free model.

One DSH headless one-shot per (model, suite). Every case gets its own working
directory (tool-use side effects stay contained and are removed on exit) and
its own UTC time window, so the ECS-side collector can join each case to the
gateway's ``chat completion`` log line and decide whether the answer came
from the primary chain or the proxy_ms fallback.

Results are appended to a JSONL file as they land (a long run must not lose
everything if it is interrupted), and a summary JSON is written at the end.

No key is stored: CUS_OC_API_KEY (or --key) is passed via the child env and
redacted from every recorded field.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from e2e_dsh_free_models import _models, _patch, _redact

DEFAULT_BASE_URL = "http://115.29.231.25:10002/v1"

SUITES: dict[str, dict[str, str]] = {
    "chat": {
        "task": "Reply with exactly the single word OK and nothing else.",
        "effort": "high",
        "expect": "OK",
    },
    "tool": {
        "task": (
            "Create a file named e2e_probe.txt containing exactly OK, then "
            "read it back with your tools and reply with only the file content."
        ),
        "effort": "high",
        "expect": "OK",
    },
    "reason": {
        "task": (
            "What is 17*23? Think step by step, then give only the final "
            "number on the last line."
        ),
        "effort": "max",
        "expect": "391",
    },
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _suite_effort(mid: str, wanted: str) -> str | None:
    """The effort to request for one case, or None when the model has none.

    Models with no declared effort ladder (big-pickle et al.) reason at their
    built-in strength; asking for a level makes DSH refuse the case outright
    (UNSUPPORTED_REASONING_EFFORT) now that capability metadata is real.
    """

    from proxy_opencode.model_profile import profile as _profile

    prof = _profile(mid)
    if prof is None:
        return wanted
    efforts = tuple(prof.capabilities.efforts)
    if not efforts:
        return None
    return wanted if wanted in efforts else efforts[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("E2E_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--dsh", default=os.environ.get("E2E_DSH_BIN", "dsh"))
    parser.add_argument("--key", default=os.environ.get("CUS_OC_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--limit", type=int, default=0, help="cap models")
    parser.add_argument(
        "--models",
        default="",
        help="comma-separated model ids to test (default: all published)",
    )
    parser.add_argument(
        "--suites", default="chat,tool,reason", help="comma-separated subset"
    )
    parser.add_argument(
        "--report",
        default=os.environ.get("E2E_TRIAD_REPORT", "e2e_dsh_triad_report.json"),
    )
    parser.add_argument(
        "--jsonl",
        default=os.environ.get("E2E_TRIAD_JSONL", "e2e_dsh_triad_cases.jsonl"),
    )
    args = parser.parse_args()
    if not args.key:
        print("CUS_OC_API_KEY / --key is required", file=sys.stderr)
        return 2
    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    unknown = [s for s in suites if s not in SUITES]
    if unknown:
        print(f"unknown suites: {unknown}", file=sys.stderr)
        return 2

    try:
        ids = _models(args.base_url, args.key)
    except (OSError, ValueError) as exc:
        print(f"cannot list models: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        ids = ids[: args.limit]
    if args.models:
        wanted = {m.strip() for m in args.models.split(",") if m.strip()}
        missing = wanted - set(ids)
        if missing:
            print(
                f"models not published by /v1/models: {sorted(missing)}",
                file=sys.stderr,
            )
            return 2
        ids = [m for m in ids if m in wanted]
    if not ids:
        print("no free bare ids published by /v1/models", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    total = len(ids) * len(suites)
    print(
        f"run_id={run_id} models={len(ids)} suites={suites} total={total}",
        flush=True,
    )
    env = {**os.environ, "CUS_OC_API_KEY": args.key}
    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("", encoding="utf-8")
    results: list[dict[str, Any]] = []
    window_start = _utc()
    index = 0

    with tempfile.TemporaryDirectory(prefix="dsh_triad_") as tmp:
        for mid in ids:
            for suite in suites:
                index += 1
                spec = SUITES[suite]
                case_id = f"{run_id}-{index:02d}"
                workdir = Path(tmp) / case_id
                workdir.mkdir(parents=True, exist_ok=True)
                patch = workdir / "patch.yml"
                effort = _suite_effort(mid, str(spec["effort"]))
                patch.write_text(
                    _patch(
                        args.base_url,
                        ids,
                        mid,
                        reasoning_effort=effort,
                    ),
                    encoding="utf-8",
                )
                started = _utc()
                t0 = time.perf_counter()
                record: dict[str, Any] = {
                    "case_id": case_id,
                    "model": mid,
                    "suite": suite,
                    "reasoning_effort": effort,
                    "started_utc": started,
                    "ended_utc": started,
                    "latency_s": 0.0,
                    "exit": None,
                    "answer": "",
                    "answer_ok": False,
                    "tool_files": [],
                    "error": "",
                }
                try:
                    proc = subprocess.run(
                        [args.dsh, "headless", "--patch", str(patch), spec["task"]],
                        env=env,
                        cwd=str(workdir),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=args.timeout,
                        check=False,
                    )
                    answer = _redact((proc.stdout or "").strip(), args.key)
                    error = _redact((proc.stderr or "").strip(), args.key)
                    record.update(
                        exit=proc.returncode,
                        answer=answer[:600],
                        answer_ok=spec["expect"] in answer,
                        error=error[-800:],
                    )
                except subprocess.TimeoutExpired:
                    record["error"] = f"timeout after {args.timeout}s"
                record["ended_utc"] = _utc()
                record["latency_s"] = round(time.perf_counter() - t0, 1)
                record["tool_files"] = sorted(
                    p.name for p in workdir.iterdir() if p.name != "patch.yml"
                )
                record["ok"] = bool(
                    record["exit"] == 0
                    and record["answer"]
                    and record["answer_ok"]
                )
                results.append(record)
                with jsonl_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(
                    f"[{index}/{total}] {mid} {suite}: "
                    f"{'PASS' if record['ok'] else 'FAIL'} "
                    f"({record['latency_s']}s, files={len(record['tool_files'])})"
                    + (f" {record['error'][-140:]}" if record["error"] else ""),
                    flush=True,
                )

    passed = sum(1 for r in results if r["ok"])
    report = {
        "run_id": run_id,
        "window_start_utc": window_start,
        "window_end_utc": _utc(),
        "base_url": args.base_url,
        "provider": "cus_oc",
        "suites": suites,
        "models": ids,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": results,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{passed}/{total} passed; report -> {args.report}")
    print(f"cases -> {jsonl_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
