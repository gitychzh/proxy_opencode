#!/usr/bin/env python3
"""End-to-end test of every published opencode free model via DSH headless.

For each bare id in ``GET /v1/models`` (the legacy ``provider/model`` alias is
skipped), build a temporary DSH patch that defines the ``cus_oc`` provider
with that catalogue and points ``agent-default-model`` at the id, then run one
one-shot task. Nothing in the user's DSH profile is modified; patches live in
a temporary directory that is removed on exit.

The API key is read from ``CUS_OC_API_KEY`` (or ``--key``), passed to the
child process through its environment and redacted from the report.

Usage:
    CUS_OC_API_KEY=... python3 scripts/e2e_dsh_free_models.py \
        --dsh /path/to/dsh.cmd --base-url http://host:10002/v1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

TASK = "Reply with exactly the single word OK and nothing else."
DEFAULT_BASE_URL = "http://115.29.231.25:10002/v1"


def _models(base_url: str, key: str) -> list[str]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ids = [
        str(entry["id"])
        for entry in body.get("data", [])
        if isinstance(entry, dict) and entry.get("id")
    ]
    # Free bare ids carry no provider prefix; the legacy alias does.
    return [mid for mid in ids if "/" not in mid]


def _dsh_model_entry(mid: str) -> str:
    """One DSH ``llm-pi-ai`` model entry from the gateway's profile table.

    Single source of truth: ``proxy_opencode.model_profile`` (models.dev
    values + live-measured floors). Unknown ids keep conservative defaults
    rather than invented capability claims.
    """

    from proxy_opencode.model_profile import profile as _profile

    prof = _profile(mid)
    if prof is None:
        context, output, efforts, attachment = 131_072, 32_768, (), False
        modalities = ["text"]
    else:
        context = prof.limits.context
        output = prof.limits.output
        efforts = tuple(prof.capabilities.efforts)
        attachment = bool(prof.capabilities.attachment)
        modalities = ["text", "image"] if attachment else ["text"]
    # DSH's headless pi-ai schema accepts only "text"/"image" entries; a
    # single "video"/"audio" item (catalog-faithful for mimo/glm/minimax)
    # poisons the WHOLE provider registration (measured 2026-11-14: one bad
    # modality -> NO_ADAPTER for every case). The catalogue's richer
    # modality list is intentionally not forwarded.
    lines = [
        f'          - id: "{mid}"',
        f"            name: {mid}",
        f"            contextWindow: {context}",
        f"            maxTokens: {output}",
        "            input:",
        *(f"              - {m}" for m in modalities),
    ]
    if efforts:
        lines.append("            reasoningEfforts:")
        lines.extend(f"              {e}: {e}" for e in efforts)
    supports_effort = "true" if efforts else "false"
    lines.extend(
        [
            "            compat:",
            "              supportsStore: false",
            "              supportsDeveloperRole: false",
            f"              supportsReasoningEffort: {supports_effort}",
            "              maxTokensField: max_tokens",
            "              thinkingFormat: zai",
        ]
    )
    return "\n".join(lines)


def _patch(
    base_url: str,
    ids: list[str],
    model: str,
    *,
    reasoning_effort: str | None = "high",
) -> str:
    entries = "\n".join(_dsh_model_entry(mid) for mid in ids)
    effort_line = (
        f"    reasoningEffort: {reasoning_effort}\n"
        if reasoning_effort is not None
        else ""
    )
    return (
        "- id: agent-default-model\n"
        '  name: "@deepseek-ai/dsh-agent-default-model"\n'
        "  config:\n"
        "    provider: cus_oc\n"
        f'    model: "{model}"\n'
        + effort_line
        + "- id: llm-pi-ai\n"
        '  name: "@deepseek-ai/dsh-llm-pi-ai"\n'
        "  config:\n"
        "    providers:\n"
        "      cus_oc:\n"
        "        displayName: cus_oc (opencode Zen free)\n"
        "        apiKeyEnv: CUS_OC_API_KEY\n"
        "        api: openai-completions\n"
        f'        baseURL: {base_url.rstrip("/")}\n'
        "        models:\n" + entries
    )


def _redact(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("E2E_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--dsh", default=os.environ.get("E2E_DSH_BIN", "dsh"))
    parser.add_argument("--key", default=os.environ.get("CUS_OC_API_KEY", ""))
    parser.add_argument(
        "--report",
        default=os.environ.get("E2E_REPORT", "e2e_dsh_free_models_report.json"),
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0, help="smoke-run cap")
    parser.add_argument("--task", default=TASK)
    args = parser.parse_args()
    if not args.key:
        print("CUS_OC_API_KEY / --key is required", file=sys.stderr)
        return 2

    try:
        ids = _models(args.base_url, args.key)
    except (OSError, ValueError) as exc:
        print(f"cannot list models: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        ids = ids[: args.limit]
    if not ids:
        print("no free bare ids published by /v1/models", file=sys.stderr)
        return 2
    print(f"models ({len(ids)}): {', '.join(ids)}", flush=True)

    env = {**os.environ, "CUS_OC_API_KEY": args.key}
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dsh_cus_oc_") as tmp:
        for index, mid in enumerate(ids, 1):
            patch = Path(tmp) / f"model_{index}.yml"
            patch.write_text(
                _patch(args.base_url, ids, mid), encoding="utf-8"
            )
            started = time.perf_counter()
            try:
                proc = subprocess.run(
                    [
                        args.dsh,
                        "headless",
                        "--patch",
                        str(patch),
                        args.task,
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=args.timeout,
                    check=False,
                )
                answer = _redact((proc.stdout or "").strip(), args.key)
                error = _redact((proc.stderr or "").strip(), args.key)
                ok = proc.returncode == 0 and bool(answer)
                record: dict[str, Any] = {
                    "model": mid,
                    "ok": ok,
                    "exit": proc.returncode,
                    "latency_s": round(time.perf_counter() - started, 1),
                    "answer": answer[:300],
                    "error": error[-500:] if not ok else "",
                }
            except subprocess.TimeoutExpired:
                record = {
                    "model": mid,
                    "ok": False,
                    "exit": None,
                    "latency_s": round(time.perf_counter() - started, 1),
                    "answer": "",
                    "error": f"timeout after {args.timeout}s",
                }
            results.append(record)
            print(
                f"[{index}/{len(ids)}] {mid}: "
                f"{'PASS' if record['ok'] else 'FAIL'} "
                f"({record['latency_s']}s)"
                + (f" {record['error'][-160:]}" if not record["ok"] else ""),
                flush=True,
            )

    passed = sum(1 for r in results if r["ok"])
    report = {
        "base_url": args.base_url,
        "provider": "cus_oc",
        "task": args.task,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{passed}/{len(results)} passed; report -> {args.report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
