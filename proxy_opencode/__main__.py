"""`python -m proxy_opencode` entrypoint."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import load_settings
from .logsetup import setup_logging


def main() -> None:
    setup_logging()
    # Build the app exactly once from the environment and hand uvicorn the
    # object: passing an import string would re-run load_settings() at import
    # time and (previously) generated a second zen session id.
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
