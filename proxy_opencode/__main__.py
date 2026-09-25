"""`python -m proxy_opencode` entrypoint."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings()
    uvicorn.run(
        "proxy_opencode.app:app",
        host="127.0.0.1",
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
