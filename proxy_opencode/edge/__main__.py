"""``python -m proxy_opencode.edge`` — the public-port process.

Runs uvicorn with the raw ASGI app (no ``--factory`` indirection: the settings
are read once here so a bad env fails at startup instead of on first request).
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .app import EdgeApp
from .config import load_edge_settings

logger = logging.getLogger("proxy_opencode.edge")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("EDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        settings = load_edge_settings()
    except ValueError as exc:
        logger.error("edge: bad configuration: %s", exc)
        return 2

    # The 2s health probe would otherwise log one httpx INFO line per poll
    # (30 lines/min of nothing). Only warnings from the transport matter here.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    port = int(os.environ.get("EDGE_PORT", "10002"))
    host = os.environ.get("EDGE_HOST", "0.0.0.0")
    logger.info(
        "edge: listening on %s:%d -> primary=%s fallback=%s model=%s mode=%s",
        host,
        port,
        settings.primary_base_url,
        settings.fallback_base_url or "(disabled)",
        settings.fallback_model,
        settings.mode,
    )
    if not settings.fallback_base_url:
        logger.warning(
            "edge: EDGE_FALLBACK_BASE_URL is empty — a primary restart will take "
            "the public port down with it"
        )
    uvicorn.run(
        EdgeApp(settings),
        host=host,
        port=port,
        log_level=os.environ.get("EDGE_UVICORN_LOG_LEVEL", "warning"),
        access_log=False,
        timeout_keep_alive=75,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
