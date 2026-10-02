#!/usr/bin/env python3
"""Per-bucket free-tier quota probe (no key material printed or stored).

For each bucket gateway this script:
  1. mints a short-lived dynamic key via POST /admin/keys (admin key comes
     from the QUOTA_PROBE_ADMIN_KEY environment variable — never hardcode it),
  2. sends ONE minimal real chat request through the bucket (so the upstream
     call egresses from the bucket's own IP — exactly what the free-tier
     quota is metered on),
  3. revokes the temporary key.

Reading the result:
    QUOTA OK   -> that bucket's egress has quota for the current UTC day
    chat -> 429 FreeUsageLimitError -> that egress's daily quota is exhausted
             (resets at the UTC day boundary, per egress IP independently)
    other      -> see the status line (auth/network/deployment problem)

Usage:
    python scripts/probe_bucket_quota.py                 # all four buckets
    python scripts/probe_bucket_quota.py win10-local     # a single bucket

The default bucket list mirrors docs/OPERATIONS.md §1; override with
QUOTA_PROBE_BUCKETS="name=url,name=url" when the topology changes.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BUCKETS = [
    ("win10-local", "http://127.0.0.1:8791"),
    ("owin10", "http://100.109.109.108:8791"),
    ("ubuntu26", "http://100.109.57.26:8791"),
    ("phone115", "http://100.87.219.115:8792"),
]

PROBE_PROMPT = "reply with exactly: pong"
MAX_TOKENS = 20  # small on purpose: one probe should cost ~1 request of quota


def _http(method: str, url: str, *, headers: dict | None = None,
          payload: dict | None = None, timeout: int = 90):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - network failures are expected here
        return None, f"{type(e).__name__}: {e}"


def probe(bucket: str, base: str, admin_key: str) -> bool:
    print(f"--- {bucket} ({base})")
    st, body = _http("POST", f"{base}/admin/keys",
                     headers={"Authorization": f"Bearer {admin_key}"},
                     payload={"name": "quota-probe-temp", "ttl_hours": 1})
    if st != 200:
        print(f"    mint key failed: {st} {body[:120]}")
        return False
    rec = json.loads(body)
    kid, key = rec["id"], rec["key"]
    try:
        st, body = _http("POST", f"{base}/v1/chat/completions",
                         headers={"Authorization": f"Bearer {key}"},
                         payload={
                             "model": "ds41f_cus",
                             "stream": False,
                             "max_tokens": MAX_TOKENS,
                             "messages": [{"role": "user", "content": PROBE_PROMPT}],
                         })
        if st == 200:
            data = json.loads(body)
            usage = data.get("usage") or {}
            leaked = "big-pickle" in body
            print(f"    QUOTA OK  chat=200 prompt_tokens={usage.get('prompt_tokens')} "
                  f"model_leak={leaked}")
            return not leaked
        if st == 429 and "FreeUsageLimitError" in body:
            print("    QUOTA EXHAUSTED (429 FreeUsageLimitError) — resets at UTC midnight")
            return False
        print(f"    chat -> {st} {body[:140].strip()}")
        return False
    finally:
        rst, _ = _http("DELETE", f"{base}/admin/keys/{kid}",
                       headers={"Authorization": f"Bearer {admin_key}"})
        print(f"    temp key revoked: {rst}")


def main() -> int:
    admin_key = os.environ.get("QUOTA_PROBE_ADMIN_KEY", "")
    if not admin_key:
        print("set QUOTA_PROBE_ADMIN_KEY (the buckets' admin key) in the environment")
        return 2
    raw = os.environ.get("QUOTA_PROBE_BUCKETS", "")
    if raw:
        buckets = [tuple(item.split("=", 1)) for item in raw.split(",") if "=" in item]
    else:
        buckets = DEFAULT_BUCKETS
    only = set(sys.argv[1:])
    ok = True
    for name, base in buckets:
        if only and name not in only:
            continue
        ok = probe(name, base, admin_key) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
