#!/usr/bin/env python3
"""Detect opencode CLI version drift and refresh whatever CAN be automated.

Why this exists: the free-tier gate is validated against the *current* CLI's
body shape. When the CLI ships a new version, the gate silently starts
answering the quota-shaped 429 — the 2026-10-29 outage was exactly that, a
1.19.5 gate being fed a 1.19.4 tool set. Catching it in CI is far cheaper
than catching it in production.

What is automated here, and what is not (the honest split):

* **Version** — npm ``opencode-ai`` dist-tag ``latest``. Free, exact.
* **Prompt template** — ``packages/opencode/src/session/prompt/default.txt``
  is plain text with no interpolation, so it can be fetched and diffed, and
  it is byte-comparable to our asset (ours is the same file plus the ``<env>``
  / skills trailer the CLI appends at runtime).
* **Builtin tool NAMES** — ``packages/core/src/tool/builtins.ts`` gives the
  modules, but the *wire* set is smaller: a live 1.19.5 capture sends 11
  names, while builtins.ts wires 12 modules (``question`` ships but is not
  offered to the model). So the source tree is only a hint; the captured set
  is the authority, and the gate tolerates a superset either way.
* **Builtin tool SCHEMAS** — *not* automatable. They are Effect ``Schema``
  constructors in TypeScript; there is no reliable static translation to JSON
  Schema. Those must come from a live capture (``scripts_local/capture_server.py``
  + the real CLI). The script reports a schema mismatch as "needs capture"
  rather than pretending to fix it.

Exit codes: 0 = in sync, 3 = drift detected, 2 = could not check (network).

Usage:
    python scripts/sync_opencode.py            # report only
    python scripts/sync_opencode.py --write    # refresh the assets it can
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "proxy_opencode" / "upstreams" / "zen" / "assets"
PROMPT_ASSET = ASSETS / "zen_prompt_default.txt"
TOOLS_ASSET = ASSETS / "zen_builtin_tools.json"

NPM_LATEST = "https://registry.npmjs.org/opencode-ai/latest"
GH_API = "https://api.github.com/repos/anomalyco/opencode"
PROMPT_PATH = "packages/opencode/src/session/prompt/default.txt"
TOOLS_PATH = "packages/core/src/tool/builtins.ts"

# Sit above the runtime-appended trailer of our asset: everything from here on
# is added by this gateway (the "<env>" block and the skills listing), so the
# comparison stops before it.
TRAILER_MARKER = "You are powered by the model named"

# Modules the CLI registers but does not offer to the model. Verified against
# a live 1.19.5 capture: builtins.ts wires 12 modules, the wire carries 11.
# `QuestionTool` backs an interactive prompt the CLI answers itself, and
# `ApplyPatchTool` is registered for a code mode that is off by default.
# A module outside this allow-list with no counterpart in our asset IS drift.
REGISTERED_NOT_OFFERED = {"question", "applypatch"}


def _get(url: str, timeout: int = 30) -> bytes:
    # Anonymous GitHub API is rate limited to 60 requests/hour per IP, which a
    # shared runner burns through instantly. A token (any classic PAT, no
    # scopes needed for public reads) lifts it to 5,000/hour; CI passes one.
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def npm_latest() -> str:
    return str(json.loads(_get(NPM_LATEST))["version"])


def gh_file(path: str, ref: str) -> str:
    payload = json.loads(_get(f"{GH_API}/contents/{path}?ref={ref}"))
    return base64.b64decode(payload["content"]).decode("utf-8")


def pinned_version() -> str:
    """The CLI version this gateway currently claims to speak."""

    from proxy_opencode.config import DEFAULT_ZEN_CLIENT_VERSION

    return DEFAULT_ZEN_CLIENT_VERSION


def builtin_names(ts_source: str) -> list[str]:
    """Candidate tool names from the ``node`` dependency list in builtins.ts.

    This is an *upper bound*, not the wire set: 1.19.5 wires 12 modules but a
    live capture sends 11 (``question`` is registered yet never offered to the
    model). Treat a missing name as a real signal and an extra one as a hint
    only — the shape gate tolerates a superset, never a subset.

    The module binding is NOT the wire name: ``TodoWriteTool`` ships as
    ``todowrite`` and ``WebFetchTool`` as ``webfetch`` (verified against the
    1.19.5 capture), so the candidates are only used to spot *new* modules.
    """

    block = ts_source.split("deps:", 1)[-1]
    found = re.findall(r"\b([A-Za-z]+)Tool\.node\b", block)
    out: list[str] = []
    for raw in found:
        if raw not in out:
            out.append(raw)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="refresh assets")
    args = ap.parse_args()

    pinned = pinned_version()
    try:
        latest = npm_latest()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cannot reach npm: {exc}", file=sys.stderr)
        return 2

    print(f"pinned={pinned} npm_latest={latest}")
    drift = latest != pinned

    try:
        official_prompt = gh_file(PROMPT_PATH, f"v{latest}")
        names = builtin_names(gh_file(TOOLS_PATH, f"v{latest}"))
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"cannot reach GitHub: {exc}", file=sys.stderr)
        if drift:
            print(f"VERSION DRIFT: pinned {pinned} != latest {latest}")
            return 3
        return 2

    ours = PROMPT_ASSET.read_text(encoding="utf-8")
    body = ours.split(TRAILER_MARKER, 1)[0]
    prompt_same = body.rstrip() == official_prompt.rstrip()
    print(f"prompt: official={len(official_prompt)} ours_body={len(body)} identical={prompt_same}")

    try:
        shipped = [
            t["function"]["name"] for t in json.loads(TOOLS_ASSET.read_text(encoding="utf-8"))
        ]
    except (OSError, KeyError, ValueError) as exc:
        print(f"cannot read tool asset: {exc}", file=sys.stderr)
        shipped = []

    # Module bindings are CamelCase and the wire names are compressed
    # (``TodoWriteTool`` -> ``todowrite``), so compare on a normalised form:
    # drop separators and case from both sides.
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    shipped_norm = {_norm(n) for n in shipped}
    unknown = [
        m for m in names if _norm(m) not in shipped_norm and _norm(m) not in REGISTERED_NOT_OFFERED
    ]
    names_ok = not unknown
    print(f"tools: source_modules={len(names)} ours={len(shipped)} unmatched={unknown or 'none'}")
    if unknown:
        print(
            "  a module with no counterpart in our asset means the CLI gained "
            "a builtin; the wire set must be re-captured (schemas are not\n"
            "  derivable from the TS source)."
        )

    if args.write and not prompt_same:
        PROMPT_ASSET.write_text(
            official_prompt.rstrip() + "\n\n" + TRAILER_MARKER + body.split(TRAILER_MARKER, 1)[1]
            if TRAILER_MARKER in ours
            else official_prompt,
            encoding="utf-8",
            newline="\n",
        )
        print(f"refreshed {PROMPT_ASSET.name}")

    if not drift and prompt_same and names_ok:
        print("in sync")
        return 0

    if drift:
        print(
            f"\nVERSION DRIFT: CLI {pinned} -> {latest}.\n"
            "  Automated: prompt template, builtin tool NAMES.\n"
            "  NOT automated: builtin tool SCHEMAS (Effect Schema in TS has no\n"
            "  reliable JSON-Schema translation). Re-capture with the real CLI:\n"
            "    python scripts_local/capture_server.py 8899 captured.json\n"
            "  then point the CLI at http://127.0.0.1:8899/v1 and replay one turn.\n"
            "  Finally bump DEFAULT_ZEN_CLIENT_VERSION in proxy_opencode/config.py."
        )
        return 3
    print(
        "\nassets drifted but version is unchanged — upstream edited a "
        "released tag; re-check the shape gate before deploying."
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
