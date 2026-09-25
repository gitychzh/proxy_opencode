"""Configuration from environment variables.

Single source of truth for all env knobs. Keep names stable; document every
one in README.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

UPSTREAM_MODES = ("openai", "opencode-serve")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# zen model ids are "provider/model", e.g. "opencode/big-pickle".
DEFAULT_SERVE_MODELS = ("opencode/big-pickle",)


@dataclass
class Settings:
    # upstream_mode selects the adapter: "openai" (plain HTTP passthrough to
    # any OpenAI-compatible endpoint) or "opencode-serve" (local official
    # `opencode serve`; the only mode that can reach Zen free models).
    upstream_mode: str = "opencode-serve"
    upstream_base_url: str = "https://api.openai.com"
    upstream_api_key: str = ""

    gateway_api_keys: list[str] = field(default_factory=list)
    port: int = 8787
    requests_per_minute: int = 60
    reasoning_passthrough: bool = True

    # opencode-serve mode (official `opencode serve` on loopback only).
    opencode_serve_url: str = "http://127.0.0.1:4096"
    opencode_server_username: str = "opencode"
    opencode_server_password: str = ""
    opencode_serve_models: list[str] = field(
        default_factory=lambda: list(DEFAULT_SERVE_MODELS)
    )
    opencode_serve_timeout_s: int = 120
    # How long one opencode agent turn may run before the gateway 504s.
    opencode_serve_wait_timeout_s: int = 600
    # Cleanup: delete opencode sessions after use (stateless bridging).
    opencode_serve_ephemeral_sessions: bool = True

    @property
    def dev_open(self) -> bool:
        """No GATEWAY_API_KEYS configured -> open dev mode (loopback only use)."""
        return len(self.gateway_api_keys) == 0

    @property
    def is_opencode_serve(self) -> bool:
        return self.upstream_mode == "opencode-serve"


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


def _validate_serve_url(url: str, password: str) -> None:
    """opencode-serve is strictly loopback-only; passwordless binds 127.0.0.1."""
    host = (urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            "OPENCODE_SERVE_URL must be loopback-only "
            f"(127.0.0.1/localhost/::1), got host {host!r}"
        )
    if not password and host != "127.0.0.1":
        raise ValueError(
            "OPENCODE_SERVE_URL with an empty OPENCODE_SERVER_PASSWORD is only "
            f"allowed on 127.0.0.1, got host {host!r}; set a password first"
        )


def load_settings() -> Settings:
    mode = os.environ.get("UPSTREAM_MODE", "opencode-serve").strip().lower()
    if mode not in UPSTREAM_MODES:
        raise ValueError(
            f"UPSTREAM_MODE must be one of {UPSTREAM_MODES}, got {mode!r}"
        )
    serve_url = os.environ.get(
        "OPENCODE_SERVE_URL", "http://127.0.0.1:4096"
    ).rstrip("/")
    serve_password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    if mode == "opencode-serve":
        _validate_serve_url(serve_url, serve_password)
    serve_models = _csv(
        os.environ.get("OPENCODE_SERVE_MODELS", ",".join(DEFAULT_SERVE_MODELS))
    )
    if not serve_models:
        serve_models = list(DEFAULT_SERVE_MODELS)
    return Settings(
        upstream_mode=mode,
        upstream_base_url=os.environ.get(
            "UPSTREAM_BASE_URL", "https://api.openai.com"
        ).rstrip("/"),
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
        gateway_api_keys=_csv(os.environ.get("GATEWAY_API_KEYS", "")),
        port=int(os.environ.get("PORT", "8787")),
        requests_per_minute=int(os.environ.get("REQUESTS_PER_MINUTE", "60")),
        reasoning_passthrough=_env_bool("REASONING_PASSTHROUGH", True),
        opencode_serve_url=serve_url,
        opencode_server_username=os.environ.get(
            "OPENCODE_SERVER_USERNAME", "opencode"
        ),
        opencode_server_password=serve_password,
        opencode_serve_models=serve_models,
        opencode_serve_timeout_s=int(
            os.environ.get("OPENCODE_SERVE_TIMEOUT_S", "120")
        ),
        opencode_serve_wait_timeout_s=int(
            os.environ.get("OPENCODE_SERVE_WAIT_TIMEOUT_S", "600")
        ),
        opencode_serve_ephemeral_sessions=_env_bool(
            "OPENCODE_SERVE_EPHEMERAL_SESSIONS", True
        ),
    )
