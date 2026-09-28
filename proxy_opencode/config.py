"""Configuration from environment variables.

Single source of truth for all env knobs. Keep names stable; document every
one in README.md.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

UPSTREAM_MODES = ("openai", "opencode-serve", "zen-direct")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# zen model ids are "provider/model", e.g. "opencode/big-pickle".
DEFAULT_SERVE_MODELS = ("opencode/big-pickle",)
DEFAULT_ZEN_MODELS = ("opencode/big-pickle",)

# Client fingerprint constants observed in a genuine opencode 1.18.32 capture
# (mitm, 2026-09). Kept as defaults so the zen-direct adapter speaks the same
# protocol; overridable for forward-compatibility when opencode releases
# newer client/runtime versions.
DEFAULT_ZEN_CLIENT_VERSION = "1.18.32"
DEFAULT_ZEN_BUN_VERSION = "1.3.14"


@dataclass
class Settings:
    # upstream_mode selects the adapter: "openai" (plain HTTP passthrough to
    # any OpenAI-compatible endpoint), "opencode-serve" (local official
    # `opencode serve`), or "zen-direct" (direct OpenAI-compatible calls to
    # OpenCode Zen with the reconstructed opencode client protocol).
    upstream_mode: str = "zen-direct"
    upstream_base_url: str = "https://api.openai.com"
    upstream_api_key: str = ""

    gateway_api_keys: list[str] = field(default_factory=list)
    # Bind address. Loopback by default; set HOST=0.0.0.0 to serve the LAN
    # (requires GATEWAY_API_KEYS — enforced in load_settings).
    host: str = "127.0.0.1"
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

    # zen-direct mode (direct OpenAI-compatible calls to OpenCode Zen).
    zen_base_url: str = "https://opencode.ai/zen/v1"
    # Empty -> anonymous free tier ("Bearer public" + system-prompt marker).
    zen_api_key: str = ""
    zen_models: list[str] = field(default_factory=lambda: list(DEFAULT_ZEN_MODELS))
    zen_timeout_s: int = 300
    # Optional egress proxy for the zen client, e.g. "http://127.0.0.1:7897".
    zen_proxy: str = ""
    # Session gate (enforced by Zen since 2026-09): anonymous free-tier
    # requests MUST carry an `x-session-id` header (any stable UUID).
    # Empty -> a fresh UUID is generated once per process.
    zen_session_id: str = ""
    zen_client_version: str = DEFAULT_ZEN_CLIENT_VERSION
    zen_bun_version: str = DEFAULT_ZEN_BUN_VERSION

    @property
    def dev_open(self) -> bool:
        """No GATEWAY_API_KEYS configured -> open dev mode (loopback only use)."""
        return len(self.gateway_api_keys) == 0

    @property
    def is_opencode_serve(self) -> bool:
        return self.upstream_mode == "opencode-serve"

    @property
    def is_zen_direct(self) -> bool:
        return self.upstream_mode == "zen-direct"


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
    mode = os.environ.get("UPSTREAM_MODE", "zen-direct").strip().lower()
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
    host = os.environ.get("HOST", "127.0.0.1").strip().lower()
    gateway_api_keys = _csv(os.environ.get("GATEWAY_API_KEYS", ""))
    if host not in LOOPBACK_HOSTS and not gateway_api_keys:
        raise ValueError(
            f"Binding a non-loopback host ({host!r}) without GATEWAY_API_KEYS "
            "would expose an unauthenticated OpenAI-compatible proxy to the "
            "network; set GATEWAY_API_KEYS first"
        )
    return Settings(
        upstream_mode=mode,
        upstream_base_url=os.environ.get(
            "UPSTREAM_BASE_URL", "https://api.openai.com"
        ).rstrip("/"),
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
        gateway_api_keys=gateway_api_keys,
        host=host,
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
        zen_base_url=os.environ.get(
            "OPENCODE_ZEN_BASE_URL", "https://opencode.ai/zen/v1"
        ).rstrip("/"),
        zen_api_key=os.environ.get("OPENCODE_ZEN_API_KEY", ""),
        zen_models=_csv(
            os.environ.get("OPENCODE_ZEN_MODELS", ",".join(DEFAULT_ZEN_MODELS))
        )
        or list(DEFAULT_ZEN_MODELS),
        zen_timeout_s=int(os.environ.get("OPENCODE_ZEN_TIMEOUT_S", "300")),
        zen_proxy=os.environ.get("ZEN_PROXY", "").strip(),
        zen_session_id=(
            os.environ.get("OPENCODE_ZEN_SESSION_ID", "").strip()
            or str(uuid.uuid4())
        ),
        zen_client_version=os.environ.get(
            "OPENCODE_ZEN_CLIENT_VERSION", DEFAULT_ZEN_CLIENT_VERSION
        ),
        zen_bun_version=os.environ.get(
            "OPENCODE_ZEN_BUN_VERSION", DEFAULT_ZEN_BUN_VERSION
        ),
    )
