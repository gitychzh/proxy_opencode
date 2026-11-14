"""Latency / stability benchmark plus reasoning-effort sweep for Zen free ids.

Two phases, both hitting the *production* gateway (ECS egress pool):

* latency: round-robin small streaming requests, one row per request with
  TTFB (first SSE byte), total time, route (usage.opencode.billing_source)
  and precise UTC stamps for later ledger JOIN;
* effort: one fixed reasoning prompt repeated at every declared effort level,
  measuring whether reasoning_effort actually changes the reasoning budget.

Failures are data, never exceptions. 429 rows keep their timestamps so the
egress A/B sentinel or a manual rotation can be correlated afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from probe_model_context import DEFAULT_MODELS, EFFORTS  # noqa: E402

REASONING_PROMPT = (
    "List every prime number below 100, then compute their sum. "
    "Work through it carefully and double-check the arithmetic before "
    "giving the final answer as SUM=<number>."
)

_LATENCY_PROMPT = "reply with the single word OK"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[idx], 3)


def _post_stream(
    base_url: str,
    key: str,
    payload: dict[str, object],
    timeout: float,
) -> tuple[int, str, str, float | None, float]:
    """Streaming call; returns (http, sse, error, ttfb_s, total_s)."""

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ttfb: float | None = None
            lines: list[str] = []
            while True:
                line = response.readline()
                if not line:
                    break
                if ttfb is None:
                    ttfb = time.perf_counter() - started
                lines.append(line.decode("utf-8", "replace"))
            return (
                response.status,
                "".join(lines),
                "",
                ttfb,
                time.perf_counter() - started,
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            "",
            exc.read().decode("utf-8", "replace"),
            None,
            time.perf_counter() - started,
        )
    except Exception as exc:  # noqa: BLE001 - transport failures are data
        return (
            0,
            "",
            f"{type(exc).__name__}: {exc}",
            None,
            time.perf_counter() - started,
        )


def _parse_sse(raw: str) -> dict[str, object]:
    usage: dict[str, object] = {}
    content_len = 0
    reasoning_len = 0
    model_echo = ""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(event.get("usage"), dict):
            usage = dict(event["usage"])  # type: ignore[arg-type]
        for choice in event.get("choices") or []:
            delta = (choice or {}).get("delta") or {}
            content_len += len(str(delta.get("content") or ""))
            reasoning_len += len(str(delta.get("reasoning_content") or ""))
        if event.get("model"):
            model_echo = str(event["model"])
    return {
        "usage": usage,
        "content_chars": content_len,
        "reasoning_chars": reasoning_len,
        "model_echo": model_echo,
    }


def _one_request(
    base_url: str,
    key: str,
    *,
    phase: str,
    model: str,
    effort: str | None,
    payload: dict[str, object],
    timeout: float,
) -> dict[str, object]:
    started_utc = _utc()
    status, sse, error, ttfb, total = _post_stream(
        base_url, key, payload, timeout
    )
    row: dict[str, object] = {
        "ts": started_utc,
        "phase": phase,
        "model": model,
        "effort": effort,
        "http": status,
        "ttfb_s": round(ttfb, 3) if ttfb is not None else None,
        "total_s": round(total, 3),
        "started_utc": started_utc,
        "ended_utc": _utc(),
    }
    if status == 200:
        agg = _parse_sse(sse)
        usage = agg["usage"]
        assert isinstance(usage, dict)
        opencode = usage.get("opencode")
        route = (
            "primary"
            if isinstance(opencode, dict)
            and opencode.get("billing_source") == "free"
            else "not-zen"
        )
        row.update(
            {
                "route": route,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "content_chars": agg["content_chars"],
                "reasoning_chars": agg["reasoning_chars"],
                "model_echo": agg["model_echo"],
            }
        )
    else:
        row["error_head"] = error[:220]
    return row


def _summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    by_model: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_model.setdefault(str(row["model"]), []).append(row)

    summary: dict[str, object] = {}
    for model, mrows in sorted(by_model.items()):
        latency_rows = [r for r in mrows if r["phase"] == "latency"]
        effort_rows = [r for r in mrows if r["phase"] == "effort"]
        oks = [r for r in latency_rows if r["http"] == 200]
        ttfbs = [float(r["ttfb_s"]) for r in oks if r.get("ttfb_s") is not None]
        totals = [float(r["total_s"]) for r in oks]
        summary[model] = {
            "latency_calls": len(latency_rows),
            "latency_ok": len(oks),
            "latency_http_errors": sorted(
                {
                    f"http {r['http']}"
                    for r in latency_rows
                    if r["http"] != 200
                }
            ),
            "ttfb_p50_s": _pct(ttfbs, 0.5),
            "ttfb_p95_s": _pct(ttfbs, 0.95),
            "total_p50_s": _pct(totals, 0.5),
            "total_p95_s": _pct(totals, 0.95),
            "primary_rate": (
                round(
                    sum(1 for r in oks if r.get("route") == "primary")
                    / len(oks),
                    3,
                )
                if oks
                else None
            ),
            "efforts": [
                {
                    "effort": r.get("effort"),
                    "http": r.get("http"),
                    "route": r.get("route", "-"),
                    "reasoning_chars": r.get("reasoning_chars"),
                    "content_chars": r.get("content_chars"),
                    "total_s": r.get("total_s"),
                    "completion_tokens": r.get("completion_tokens"),
                }
                for r in effort_rows
            ],
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://115.29.231.25:10002/v1")
    parser.add_argument("--key-env", default="CUS_OC_API_KEY")
    parser.add_argument(
        "--models", default=",".join(DEFAULT_MODELS)
    )
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--gap", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--no-effort-sweep", action="store_true"
    )
    parser.add_argument("--out", default="")
    parser.add_argument("--summary", default="")
    args = parser.parse_args(argv)

    key = os.environ.get(args.key_env, "").strip()
    if not key:
        print(f"environment {args.key_env} is empty", file=sys.stderr)
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("", encoding="utf-8")

    rows: list[dict[str, object]] = []

    def _emit(row: dict[str, object]) -> None:
        rows.append(row)
        if out_path:
            with out_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"[{row['phase']}] {row['model']}"
            + (f" effort={row['effort']}" if row.get("effort") else "")
            + f" http={row['http']}"
            + f" ttfb={row.get('ttfb_s')}"
            + f" total={row['total_s']}"
            + (
                f" route={row.get('route')} reason={row.get('reasoning_chars')}"
                if row["http"] == 200
                else f" err={str(row.get('error_head', ''))[:80]}"
            ),
            flush=True,
        )

    # ---- phase 1: latency / stability, round-robin ------------------------
    for rnd in range(1, args.rounds + 1):
        for model in models:
            payload: dict[str, object] = {
                "model": model,
                "messages": [
                    {"role": "user", "content": _LATENCY_PROMPT}
                ],
                "stream": True,
            }
            _emit(
                _one_request(
                    args.base_url,
                    key,
                    phase="latency",
                    model=model,
                    effort=None,
                    payload=payload,
                    timeout=args.timeout,
                )
            )
            if args.gap > 0:
                time.sleep(args.gap)

    # ---- phase 2: reasoning-effort sweep ----------------------------------
    if not args.no_effort_sweep:
        for model in models:
            levels = EFFORTS.get(model, ())
            if not levels:
                print(
                    f"[effort] {model}: no declared ladder, skipped",
                    flush=True,
                )
                continue
            for level in levels:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "user", "content": REASONING_PROMPT}
                    ],
                    "stream": True,
                    "max_tokens": 4096,
                    "reasoning_effort": level,
                }
                _emit(
                    _one_request(
                        args.base_url,
                        key,
                        phase="effort",
                        model=model,
                        effort=level,
                        payload=payload,
                        timeout=max(args.timeout, 300.0),
                    )
                )
                if args.gap > 0:
                    time.sleep(args.gap)

    summary = _summarize(rows)
    text = json.dumps(
        {
            "generated_at": _utc(),
            "base_url": args.base_url,
            "rounds": args.rounds,
            "summary": summary,
        },
        ensure_ascii=False,
        indent=1,
    )
    if args.summary:
        sum_path = Path(args.summary)
        sum_path.parent.mkdir(parents=True, exist_ok=True)
        sum_path.write_text(text, encoding="utf-8")
        print(f"summary written: {sum_path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
