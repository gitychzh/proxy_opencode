#!/usr/bin/env python3
"""Daily opencode free-model sync for the ECS host (systemd timer, 22:00).

Anonymous GET of the upstream model catalogue -> free bare ids -> compare
with the last snapshot. When the set changed, atomically rewrite the snapshot
on the /data volume (the gateway hot-reloads it by mtime, no restart) and
update ``OPENCODE_ZEN_MODELS`` in .env so a later container rebuild keeps the
same exit configuration. When nothing changed, no file is written at all.

Exit codes: 0 = unchanged or synced, 2 = upstream/IO failure.

Usage:
    python3 scripts/sync_zen_free_models.py                # normal run
    python3 scripts/sync_zen_free_models.py --dry-run      # report only
    python3 scripts/sync_zen_free_models.py --json         # machine readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from proxy_opencode.egress.free_models import (  # noqa: E402
    DEFAULT_BASE_URL,
    sync_free_models,
)

DEFAULT_STATE_FILE = "/opt/zen-gw/src/deploy/ecs/data/zen_free_models.json"
DEFAULT_ENV_FILE = "/opt/zen-gw/src/deploy/ecs/.env"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-file",
        default=os.environ.get("ZEN_FREE_MODELS_STATE_FILE", DEFAULT_STATE_FILE),
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("ZEN_FREE_MODELS_ENV_FILE", DEFAULT_ENV_FILE),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENCODE_ZEN_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("OPENCODE_ZEN_API_KEY", "")
    )
    parser.add_argument(
        "--no-env", action="store_true", help="refresh the snapshot only"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the diff, write nothing"
    )
    parser.add_argument("--json", action="store_true", help="one-line JSON summary")
    args = parser.parse_args()

    try:
        result = sync_free_models(
            state_file=args.state_file,
            env_file=None if args.no_env else args.env_file,
            base_url=args.base_url,
            api_key=args.api_key,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(
            f"free-model sync failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    payload = {
        "changed": result.changed,
        "reason": result.reason,
        "count": len(result.ids),
        "added": result.added,
        "removed": result.removed,
        "state_written": result.state_written,
        "env_written": result.env_written,
        "ids": result.ids,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif result.changed:
        print(
            f"free models {result.reason}: count={len(result.ids)} "
            f"added={result.added} removed={result.removed} "
            f"state_written={result.state_written} "
            f"env_written={result.env_written}"
        )
    else:
        print(
            f"free models unchanged: count={len(result.ids)} - "
            "nothing written, nothing restarted"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
