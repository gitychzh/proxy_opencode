"""End-to-end validation: drive the gateway from real hermes runs.

Runs N one-shot hermes queries against the `proxyo` provider (which points at
http://127.0.0.1:8787/v1, i.e. proxy_opencode -> opencode serve -> Zen
big-pickle). Mixes plain Q&A, reasoning prompts and tool-using prompts.
Writes a JSON report to cap/e2e_report.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

HERMES = r"C:\Users\ychzh\AppData\Local\hermes\bin\hermes.exe"
WORKDIR = r"C:\Users\ychzh\Documents\ChatGPT\cap\proj"
REPORT = r"C:\Users\ychzh\Documents\ChatGPT\cap\e2e_report.json"

COMMON = [HERMES, "chat", "--provider", "proxyo", "-m", "opencode/big-pickle",
          "--oneshot", "--cli", "--in", WORKDIR]
TOOL_FLAGS = ["-t", "terminal", "-t", "files", "--yolo"]

PROMPTS = [
    # plain conversation
    ("plain", "Reply with exactly: pong", []),
    ("plain", "用一句话介绍光合作用。", []),
    ("plain", "What is the capital of Japan? One word.", []),
    ("plain", "Name three primary colors.", []),
    ("plain", "Say 'ready' and nothing else.", []),
    # math / reasoning
    ("reason", "What is 17*23? Think step by step, then give the final number.", ["--reasoning", "high"]),
    ("reason", "If a train travels 120 km in 1.5 hours, what is its average speed in km/h? Show reasoning.", ["--reasoning", "high"]),
    ("reason", "Compute 2^10 + 3^4. Reason briefly.", ["--reasoning", "low"]),
    ("reason", "A shirt costs $25 after a 20% discount. What was the original price? Reason it out.", ["--reasoning", "high"]),
    ("reason", "Which is larger: 0.9^10 or 0.8^8? Reason first.", ["--reasoning", "medium"]),
    # tool use: create + read files, run commands
    ("tool", "Create a file named hello.txt containing 'hello world', then confirm by reading it back.", TOOL_FLAGS),
    ("tool", "Run a command to list files in the current directory and report the count.", TOOL_FLAGS),
    ("tool", "Write a Python script fizz.py that prints FizzBuzz 1..15, run it, paste the output.", TOOL_FLAGS),
    ("tool", "Create data.json with {\"a\":1,\"b\":2}, then run a python one-liner that sums the values and reports the result.", TOOL_FLAGS),
    ("tool", "Use the terminal to echo 'file-ok' into check.txt and read the file to verify.", TOOL_FLAGS),
]

def main() -> int:
    n_target = 36
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
    for idx, (kind, prompt, extra) in enumerate(jobs, 1):
        cmd = COMMON + ["-q", prompt] + extra
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=420,
                              encoding="utf-8", errors="replace")
        dt = round(time.time() - t0, 1)
        out = proc.stdout + proc.stderr
        good = proc.returncode == 0 and "HTTP 4" not in out and "HTTP 5" not in out
        has_tool = "tool call" in out.lower() or "Tool:" in out or "\u256d" in out and ("ran" in out.lower() or "tool" in out.lower())
        has_reason = "Reasoning" in out
        ok += bool(good); tools_seen += bool(has_tool); reasoning_seen += bool(has_reason)
        fail += not good
        print(f"[{idx}/{n_target}] {kind} rc={proc.returncode} {dt}s tool={has_tool} reason={has_reason}")
        results.append({"idx": idx, "kind": kind, "prompt": prompt, "rc": proc.returncode,
                        "elapsed_s": dt, "ok": good, "tool_used": has_tool,
                        "reasoning": has_reason,
                        "tail": out[-400:]})
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=1)
    print(f"DONE ok={ok} fail={fail} tools_seen={tools_seen} reasoning_seen={reasoning_seen}")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
