"""End-to-end validation: drive the gateway from real hermes runs.

Runs N one-shot hermes queries against the configured hermes provider (which
must point at the gateway, e.g. http://127.0.0.1:8787/v1) and writes a JSON
report. Mixes plain Q&A, reasoning prompts and tool-using prompts.

Everything machine-specific is overridable by environment variable so the
script is portable across the buckets:

    E2E_HERMES_BIN   hermes executable            (default: PATH lookup)
    E2E_PROVIDER     hermes provider name          (default: proxyo)
    E2E_MODEL        model id sent to hermes       (default: ds41f_cus)
    E2E_WORKDIR      tool-using sandbox directory  (default: cwd)
    E2E_REPORT       report output path            (default: ./e2e_report.json)
    E2E_N            number of jobs to run         (default: 36)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

HERMES = os.environ.get("E2E_HERMES_BIN") or shutil.which("hermes") or "hermes"
WORKDIR = os.environ.get("E2E_WORKDIR", os.getcwd())
REPORT = os.environ.get("E2E_REPORT", os.path.join(os.getcwd(), "e2e_report.json"))
PROVIDER = os.environ.get("E2E_PROVIDER", "proxyo")
MODEL = os.environ.get("E2E_MODEL", "ds41f_cus")
N_TARGET = int(os.environ.get("E2E_N", "36"))

COMMON = [HERMES, "chat", "--provider", PROVIDER, "-m", MODEL,
          "--oneshot", "--cli", "--in", WORKDIR]
# hermes toolset names are singular: `terminal` and `file` (NOT `files`).
# `-t files` silently yields "Unknown toolset" and hermes then loads ZERO
# tools — the model still answers, so the failure is invisible unless the
# tool-using cases actually check that a file landed on disk.
TOOL_FLAGS = ["-t", "terminal", "-t", "file", "--yolo"]

PROMPTS = [
    # plain conversation
    ("plain", "Reply with exactly: pong", []),
    ("plain", "用一句话介绍光合作用。", []),
    ("plain", "What is the capital of Japan? One word.", []),
    ("plain", "Name three primary colors.", []),
    ("plain", "Say 'ready' and nothing else.", []),
    # math / reasoning
    ("reason",
     "What is 17*23? Think step by step, then give the final number.",
     ["--reasoning", "high"]),
    ("reason",
     "If a train travels 120 km in 1.5 hours, what is its average speed in km/h?"
     " Show reasoning.",
     ["--reasoning", "high"]),
    ("reason", "Compute 2^10 + 3^4. Reason briefly.", ["--reasoning", "low"]),
    ("reason",
     "A shirt costs $25 after a 20% discount. What was the original price?"
     " Reason it out.",
     ["--reasoning", "high"]),
    ("reason",
     "Which is larger: 0.9^10 or 0.8^8? Reason first.",
     ["--reasoning", "medium"]),
    # tool use: create + read files, run commands
    ("tool",
     "Create a file named hello.txt containing 'hello world', then confirm by"
     " reading it back.",
     TOOL_FLAGS),
    ("tool",
     "Run a command to list files in the current directory and report the"
     " count.",
     TOOL_FLAGS),
    ("tool",
     "Write a Python script fizz.py that prints FizzBuzz 1..15, run it, paste"
     " the output.",
     TOOL_FLAGS),
    ("tool",
     "Create data.json with {\"a\":1,\"b\":2}, then run a python one-liner that"
     " sums the values and reports the result.",
     TOOL_FLAGS),
    ("tool",
     "Use the terminal to echo 'file-ok' into check.txt and read the file to"
     " verify.",
     TOOL_FLAGS),
]

def _workdir_files() -> set[str]:
    try:
        return set(os.listdir(WORKDIR))
    except OSError:
        return set()


def main() -> int:
    if not (os.path.exists(HERMES) or shutil.which(HERMES)):
        print(
            f"hermes executable not found: {HERMES!r}\n"
            "Install hermes or set E2E_HERMES_BIN to its full path.",
            file=sys.stderr,
        )
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(REPORT)), exist_ok=True)
    n_target = N_TARGET
    jobs = []
    i = 0
    while len(jobs) < n_target:
        kind, prompt, extra = PROMPTS[i % len(PROMPTS)]
        # vary repeats slightly so cache-free turns
        if i >= len(PROMPTS):
            prompt = prompt + f" (variation {i // len(PROMPTS)})"
        jobs.append((kind, prompt, extra))
        i += 1

    results = []
    ok = fail = tools_seen = reasoning_seen = 0
    tool_jobs = tool_files = 0
    for idx, (kind, prompt, extra) in enumerate(jobs, 1):
        cmd = COMMON + ["-q", prompt] + extra
        before = _workdir_files() if kind == "tool" else set()
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=420,
                              encoding="utf-8", errors="replace")
        dt = round(time.time() - t0, 1)
        out = proc.stdout + proc.stderr
        good = proc.returncode == 0 and "HTTP 4" not in out and "HTTP 5" not in out
        has_tool = (
            "tool call" in out.lower()
            or "Tool:" in out
            or ("\u256d" in out and ("ran" in out.lower() or "tool" in out.lower()))
        )
        has_reason = "Reasoning" in out
        # Ground truth for tool jobs: did anything actually land on disk?
        # (a mis-named toolset makes hermes load 0 tools while still replying
        # politely, so the text heuristic alone can't catch it)
        created = sorted(_workdir_files() - before) if kind == "tool" else []
        if kind == "tool":
            tool_jobs += 1
            tool_files += len(created)
        ok += bool(good)
        tools_seen += bool(has_tool)
        reasoning_seen += bool(has_reason)
        fail += not good
        print(f"[{idx}/{n_target}] {kind} rc={proc.returncode} {dt}s"
              f" tool={has_tool} reason={has_reason} files={created}")
        results.append({
            "idx": idx, "kind": kind, "prompt": prompt, "rc": proc.returncode,
            "elapsed_s": dt, "ok": good, "tool_used": has_tool,
            "reasoning": has_reason, "files_created": created,
            "tail": out[-400:],
        })
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=1)
    if tool_jobs and tool_files == 0:
        print(
            "FAIL: none of the tool jobs produced a file — check the hermes "
            "toolset flags (must be `-t terminal -t file`; `files` is invalid "
            "and silently loads zero tools)",
            file=sys.stderr,
        )
        fail += 1
    print(f"DONE ok={ok} fail={fail} tools_seen={tools_seen} "
          f"reasoning_seen={reasoning_seen} tool_files={tool_files}")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
