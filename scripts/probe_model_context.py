"""Empirical context-window probe for opencode Zen free models.

Sends filler-padded streaming chat requests through a *local* gateway running
with ``ZEN_CONTEXT_GUARD=0`` (measuring the provider, not our guard) and
reports, per model:

* the largest prompt the upstream actually accepted (``usage.prompt_tokens``);
* the ceiling the upstream itself cites in rejections
  ("maximum context length of (N)");
* per declared effort level, whether ``reasoning_effort`` is accepted and
  whether reasoning deltas come back (needs ``ZEN_REASONING_POLICY=client``).

Every accepted request burns one free-tier call for the probing egress IP, so
the ladder is deliberately short: it walks fractions of the *declared* window
(models.dev) and extends only while the upstream keeps accepting. Rejection
messages usually name the exact ceiling, which ends the search early.

Route honesty: a data point counts only when
``usage.opencode.billing_source == "free"`` (primary chain); anything else is
recorded as ``route != primary`` and excluded from conclusions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODELS = (
    "big-pickle",
    "minimax-m3.1-flash-free",
    "glm-5.3-flash-free",
    "deepseek-v4.1-flash-free",
)

# models.dev provider "opencode", 2026-11-14 (cross-checked against the
# opencode 1.19.5 binary snapshot). big-pickle's declared window is stale —
# live upstream rejects past 262139 (measured 2026-10-29, twice).
DECLARED: dict[str, dict[str, int]] = {
    "big-pickle": {"context": 262_139, "output": 32_000},
    "mimo-v2.6-flash-free": {"context": 200_000, "output": 32_000},
    "deepseek-v4.1-flash-free": {"context": 200_000, "output": 32_000},
    "glm-5.3-flash-free": {"context": 200_000, "output": 32_000},
    "longcat-2.5-free": {"context": 1_000_000, "output": 262_144},
    "iceman-free": {"context": 262_144, "output": 131_072},
    "inkling-2-small-free": {"context": 1_048_576, "output": 131_072},
    "beam-free": {"context": 1_048_576, "output": 131_072},
    "flint-40b-free": {"context": 262_144, "output": 65_536},
    "nemotron-3.5-super-vl-free": {"context": 1_048_576, "output": 131_072},
    "nemotron-3.5-lightning-free": {"context": 262_144, "output": 262_144},
    "minimax-m3.1-flash-free": {"context": 1_000_000, "output": 512_000},
}

EFFORTS: dict[str, tuple[str, ...]] = {
    "minimax-m3.1-flash-free": ("low", "medium", "high", "xhigh", "max"),
    "glm-5.3-flash-free": ("low", "high", "max"),
    "deepseek-v4.1-flash-free": ("low", "high", "max"),
    "inkling-2-small-free": ("none", "minimal", "low", "medium", "high", "max"),
    "beam-free": ("low", "medium", "high", "xhigh", "max"),
    "flint-40b-free": ("low", "medium", "high", "xhigh", "max"),
    "nemotron-3.5-super-vl-free": (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ),
}

_CEILING_RE = re.compile(r"maximum context length(?:\s*of)?\s*\(?\s*(\d+)", re.I)
_MAX_PROBES = 8


def _filler(target_tokens: int, slope: float, base: int) -> str:
    """Repetitions calibrated so prompt ≈ target tokens (slope fitted in-run)."""

    room = max(target_tokens - base, 8)
    reps = max(8, int(room / slope)) if slope > 0 else max(8, target_tokens)
    return "a " * reps


def _post_stream(
    base_url: str,
    key: str,
    payload: dict[str, object],
    timeout: float,
) -> tuple[int, str, str, float]:
    """One gateway call; returns (http, sse_text_or_, error_text, latency)."""

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
            text = response.read().decode("utf-8", "replace")
            return response.status, text, "", time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            "",
            exc.read().decode("utf-8", "replace"),
            time.perf_counter() - started,
        )
    except Exception as exc:  # noqa: BLE001 - transport failures are data
        return (
            0,
            "",
            f"{type(exc).__name__}: {exc}",
            time.perf_counter() - started,
        )


def _parse_sse(raw: str) -> dict[str, object]:
    """Reassemble one SSE body into a final aggregate dict (usage etc.)."""

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


def _probe_once(
    base_url: str,
    key: str,
    model: str,
    target_tokens: int,
    timeout: float,
    effort: str | None = None,
    slope: float = 1.0,
    base: int = 0,
) -> dict[str, object]:
    """One ladder step; never raises (failures are data points too)."""

    message = (
        _filler(target_tokens, slope, base)
        + "\n\nIgnore the repeated text above entirely. "
        "Reply with the single word OK."
    )
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "stream": True,
    }
    if effort is not None:
        payload["reasoning_effort"] = effort
    status, sse_text, error_text, latency = _post_stream(
        base_url, key, payload, timeout
    )

    point: dict[str, object] = {
        "model": model,
        "target_tokens": target_tokens,
        "effort": effort,
        "http": status,
        "latency_s": round(latency, 2),
    }
    if status == 200:
        agg = _parse_sse(sse_text)
        usage = agg["usage"]
        assert isinstance(usage, dict)
        opencode = usage.get("opencode")
        route = (
            "primary"
            if isinstance(opencode, dict)
            and opencode.get("billing_source") == "free"
            else "not-zen"
        )
        point.update(
            {
                "kind": "accept",
                "route": route,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "content_chars": agg["content_chars"],
                "reasoning_chars": agg["reasoning_chars"],
                "model_echo": agg["model_echo"],
            }
        )
        return point

    match = _CEILING_RE.search(error_text)
    point.update(
        {
            "kind": "reject" if status else "transport-error",
            "error_head": error_text[:220],
            "cited_ceiling": int(match.group(1)) if match else None,
        }
    )
    return point


def calibrate(
    base_url: str,
    key: str,
    model: str,
    timeout: float,
) -> tuple[float, int, list[dict[str, object]]]:
    """Two tiny probes fit tokens-per-repetition and the fixed prompt base.

    The gateway injects its marker prompt and builtin tools into every body,
    so raw repetition counts are offset by a constant; the slope/intercept
    pair makes later ladder targets land where intended.
    """

    points = [
        _probe_once(base_url, key, model, low, timeout, slope=1.0, base=0)
        for low in (1_000, 4_000)
    ]
    got = [
        int(p.get("prompt_tokens") or 0)
        for p in points
        if p.get("kind") == "accept" and p.get("route") == "primary"
    ]
    if len(got) < 2 or got[1] <= got[0]:
        return 1.0, 0, points  # fall back to uncalibrated 1:1
    slope = (got[1] - got[0]) / 3_000.0
    base = got[0] - int(slope * 1_000)
    return max(slope, 0.05), max(base, 0), points


_QUOTA_MARKERS = ("usage limit", "freeusagelimit", "rate limit", "too many requests")


def probe_model(
    base_url: str,
    key: str,
    model: str,
    *,
    fractions: tuple[float, ...],
    timeout: float,
    effort_sweep: bool,
    slope: float = 1.0,
    base: int = 0,
) -> dict[str, object]:
    declared = DECLARED[model]["context"]
    targets = sorted({max(64, int(declared * f)) for f in fractions})
    points: list[dict[str, object]] = []
    accepted: list[int] = []
    ceilings: list[int] = []

    index = 0
    extensions = 0
    consecutive_rejects = 0
    quota_blocked = False
    while index < len(targets) and len(points) < _MAX_PROBES:
        target = targets[index]
        point = _probe_once(
            base_url, key, model, target, timeout, slope=slope, base=base
        )
        points.append(point)
        kind = str(point.get("kind"))
        if kind == "accept" and point.get("route") == "primary":
            accepted.append(int(point.get("prompt_tokens") or 0))
            consecutive_rejects = 0
            # One ladder extension while the upstream keeps accepting.
            if index == len(targets) - 1 and extensions < 1:
                targets.extend([int(target * 1.15), int(target * 1.3)])
                extensions += 1
        elif kind == "reject":
            consecutive_rejects += 1
            head = str(point.get("error_head") or "").lower()
            if any(marker in head for marker in _QUOTA_MARKERS):
                quota_blocked = True
            cited = point.get("cited_ceiling")
            if isinstance(cited, int):
                # The provider named its exact window — search is over.
                ceilings.append(cited)
                break
            if consecutive_rejects >= 2:
                break
        else:
            break  # transport error — probing this model is pointless now
        index += 1

    efforts_report: list[dict[str, object]] = []
    if effort_sweep and not quota_blocked:
        for level in EFFORTS.get(model, ()):
            efforts_report.append(
                _probe_once(
                    base_url, key, model, 512, timeout,
                    effort=level, slope=slope, base=base,
                )
            )

    return {
        "model": model,
        "declared_context": declared,
        "points": points,
        "efforts": efforts_report,
        "measured_max_accept_prompt_tokens": max(accepted) if accepted else None,
        "cited_ceilings": sorted(set(ceilings)),
        "accepted_calls": len(accepted),
        "primary_points": sum(1 for p in points if p.get("route") == "primary"),
        "quota_blocked": quota_blocked,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8788/v1")
    parser.add_argument("--key-env", default="CUS_OC_API_KEY")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="comma-separated model ids",
    )
    parser.add_argument(
        "--fractions",
        default="0.85,1.0,1.08",
        help="ladder fractions of the declared window",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--no-calibrate",
        action="store_true",
        help="skip the two-point filler calibration (already known)",
    )
    parser.add_argument(
        "--effort-sweep",
        action="store_true",
        help="also probe each declared reasoning effort level",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    key = os.environ.get(args.key_env, "").strip()
    if not key:
        print(f"environment {args.key_env} is empty", file=sys.stderr)
        return 2
    fractions = tuple(float(x) for x in args.fractions.split(",") if x.strip())
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [m for m in models if m not in DECLARED]
    if unknown:
        print(f"unknown models (add to DECLARED): {unknown}", file=sys.stderr)
        return 2

    models_report: dict[str, object] = {}
    slope, base = 1.0, 0
    calib_points: list[dict[str, object]] = []
    if not args.no_calibrate:
        print("=== calibrating filler slope/base on first model ===", flush=True)
        slope, base, calib_points = calibrate(
            args.base_url, key, models[0], args.timeout
        )
        print(f"  slope={slope:.4f} tokens/rep base={base} tokens", flush=True)
    for model in models:
        print(f"=== probing {model} ===", flush=True)
        result = probe_model(
            args.base_url,
            key,
            model,
            fractions=fractions,
            timeout=args.timeout,
            effort_sweep=args.effort_sweep,
            slope=slope,
            base=base,
        )
        models_report[model] = result
        print(
            f"  accepted_max={result['measured_max_accept_prompt_tokens']} "
            f"cited={result['cited_ceilings']} "
            f"calls={result['accepted_calls']}"
            + (" QUOTA_BLOCKED" if result["quota_blocked"] else ""),
            flush=True,
        )

    report = {
        "base_url": args.base_url,
        "fractions": list(fractions),
        "calibration": {"slope": slope, "base": base, "points": calib_points},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "models": models_report,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"report written: {out_path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
