"""`python -m proxy_opencode` entrypoint."""

from __future__ import annotations

import uvicorn

from .config import load_settings
from .logsetup import setup_logging


def main() -> None:
    setup_logging()
    settings = load_settings()
    uvicorn.run(
        "proxy_opencode.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
