#!/usr/bin/env python3
"""Egress A/B sentinel: catch a 429 wave red-handed, per the attribution rule.

The gateway 429s in episodic waves (all buckets within seconds, self-recovery
inside the hour). The 2026-11-14 iron rule: a wave may only be called "real
upstream quota" after an opencode CLI golden-standard request through the SAME
bucket agent gets 429 too; CLI 200 in the same window means a gateway bug.

Loop:
  1. one minimal gateway chat request (big-pickle);
  2. read the ledger rows for that window via ssh;
  3. heartbeat row when primary is healthy;
  4. on any http_429 bucket: run the CLI through THAT bucket's agent proxy
     within the same minute and append the A/B verdict;
  5. --auto-rotate additionally calls the admin rotation endpoint when the
     CLI itself got 429 (quota verified; rotation then renews the IP).

Secrets (gateway key, egress secret) are read once, kept in memory, and
redacted from every output row. Evidence lands in --out as JSONL.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ECS = "root@115.29.231.25"
ECS_ENV = "/opt/zen-gw/src/deploy/ecs/.env"
ECS_LEDGER = "/opt/zen-gw/src/deploy/ecs/data/egress_events.jsonl"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ssh(script: str, timeout: float = 30.0) -> str:
    proc = subprocess.run(
        ["ssh", "-p", "222", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
         ECS, script],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _read_env() -> tuple[str, str, dict[str, str]]:
    """Gateway key, egress secret, bucket-name -> proxy URL (no printing)."""

    raw = _ssh(f"grep -E '^(GATEWAY_API_KEYS|ZEN_EGRESS_SECRET|ZEN_EGRESS_PROXIES)=' {ECS_ENV}")
    key = secret = ""
    proxies: dict[str, str] = {}
    for line in raw.splitlines():
        name, _, value = line.strip().partition("=")
        if name == "GATEWAY_API_KEYS":
            key = value.split(",")[0].strip()
        elif name == "ZEN_EGRESS_SECRET":
            secret = value.strip()
        elif name == "ZEN_EGRESS_PROXIES":
            for part in value.split(","):
                bucket, _, url = part.partition("=")
                if bucket and url:
                    proxies[bucket.strip()] = url.strip()
    if not key or not secret or not proxies:
        raise SystemExit(
            "could not read GATEWAY_API_KEYS / ZEN_EGRESS_SECRET /"
            " ZEN_EGRESS_PROXIES from ECS"
        )
    return key, secret, proxies


def _gateway_probe(
    base_url: str, key: str, model: str, timeout: float
) -> tuple[int, float]:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "reply with the single word OK"}],
            "stream": True,
        }
    ).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return resp.status, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.perf_counter() - started
    except Exception:  # noqa: BLE001 - transport failure is a heartbeat too
        return 0, time.perf_counter() - started


def _ledger_rows(window_start: str, window_end: str) -> list[dict[str, str]]:
    script = (
        "python3 -c \"\n"
        "import json\n"
        f"rows=[json.loads(l) for l in open('{ECS_LEDGER}',encoding='utf-8')]\n"
        f"a,b='{window_start}','{window_end}'\n"
        "for r in rows:\n"
        "    if r.get('event')=='egress_request' and a<=r.get('ts','')<=b:\n"
        "        keys=('ts','egress','kind','ok','egress_ip','model','request_id')\n"
        "        print(json.dumps({k:r.get(k) for k in keys}))\n"
        "\""
    )
    out = _ssh(script, timeout=30.0)
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _cli_golden(proxy_url: str, timeout: float) -> tuple[str, str]:
    """opencode CLI through one bucket agent; returns (verdict, output_tail)."""

    env = {**os.environ, "HTTPS_PROXY": proxy_url}
    try:
        proc = subprocess.run(
            ["opencode", "run", "reply with the single word OK",
             "--model", "opencode/big-pickle"],
            env=env, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout", ""
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0 and "OK" in out:
        return "ok", out[-200:]
    lowered = out.lower()
    if "429" in lowered or "usage limit" in lowered or "rate limit" in lowered:
        return "429", out[-300:]
    if proc.returncode == 0:
        return "ok", out[-200:]
    return "error", out[-300:]


def _admin_rotate(bucket: str, timeout: float = 60.0) -> str:
    out = _ssh(
        "cd /opt/zen-gw/src/deploy/ecs && ADM=$(grep '^ADMIN_API_KEYS=' .env "
        "| cut -d= -f2- | cut -d, -f1 | tr -d '\\r\\n') && "
        f"curl -sS -m 55 -X POST -H 'Authorization: Bearer $ADM' "
        f"http://127.0.0.1:10002/admin/egresses/{bucket}/rotate",
        timeout=timeout,
    )
    return out.strip()[-200:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://115.29.231.25:10002/v1")
    parser.add_argument("--key-env", default="CUS_OC_API_KEY")
    parser.add_argument("--model", default="big-pickle")
    parser.add_argument("--interval", type=float, default=180.0)
    parser.add_argument("--loops", type=int, default=0, help="0 = run forever")
    parser.add_argument("--cli-timeout", type=float, default=110.0)
    parser.add_argument(
        "--auto-rotate", action="store_true",
        help="rotate a bucket via admin API when the CLI confirms its 429",
    )
    parser.add_argument("--out", default="egress_ab_sentinel.jsonl")
    args = parser.parse_args(argv)

    key = os.environ.get(args.key_env, "").strip() or _read_env()[0]
    _, secret, proxies = _read_env()
    secrets = (key, secret)
    out_path = Path(args.out)
    print(f"sentinel -> {out_path} buckets={sorted(proxies)}", flush=True)

    loop = 0
    while True:
        loop += 1
        started = _utc()
        status, latency = _gateway_probe(args.base_url, key, args.model, 150.0)
        ended = _utc()
        rows = _ledger_rows(started, ended)
        fours = [r for r in rows if str(r.get("kind")) == "http_429"]
        record: dict[str, object] = {
            "ts": started,
            "loop": loop,
            "gateway_status": status,
            "latency_s": round(latency, 2),
            "ledger": [
                {k: r.get(k) for k in ("ts", "egress", "kind", "ok", "egress_ip")}
                for r in rows
            ],
        }
        if not fours:
            record["verdict"] = "primary-ok" if status == 200 else f"http-{status}"
            print(
                f"[{loop}] {started} gw={status} {record['verdict']}",
                flush=True,
            )
        else:
            buckets = sorted({str(r.get("egress")) for r in fours})
            record["verdict"] = "429-wave"
            record["wave_buckets"] = buckets
            print(f"[{loop}] {started} gw={status} 429-WAVE on {buckets}", flush=True)
            for bucket in buckets:
                proxy = proxies.get(bucket, "")
                if not proxy:
                    record[f"cli_{bucket}"] = "no-proxy-url"
                    continue
                cli_verdict, cli_tail = _cli_golden(proxy, args.cli_timeout)
                cli_tail = _redact(cli_tail, *secrets)
                record[f"cli_{bucket}"] = cli_verdict
                record[f"cli_{bucket}_tail"] = cli_tail
                same_ip = next(
                    (r.get("egress_ip") for r in fours
                     if str(r.get("egress")) == bucket),
                    "",
                )
                ab = (
                    "GATEWAY-BUG (cli ok on same ip)"
                    if cli_verdict == "ok"
                    else "upstream-throttle-confirmed"
                    if cli_verdict == "429"
                    else f"cli-{cli_verdict}"
                )
                record[f"ab_{bucket}"] = ab
                print(
                    f"    {bucket} ip={same_ip} cli={cli_verdict} -> {ab}",
                    flush=True,
                )
                if cli_verdict == "429" and args.auto_rotate:
                    rot = _redact(_admin_rotate(bucket), *secrets)
                    record[f"rotate_{bucket}"] = rot
                    print(f"    {bucket} rotate -> {rot}", flush=True)

        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        if args.loops and loop >= args.loops:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
