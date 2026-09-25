"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

UPSTREAM_MODES = ("openai", "opencode-cli")


@dataclass
class Settings:
    upstream_mode: str = "openai"
    upstream_base_url: str = "https://api.openai.com"
    upstream_api_key: str = ""
    gateway_api_keys: list[str] = field(default_factory=list)
    port: int = 8787
    reasoning_passthrough: bool = True
    requests_per_minute: int = 60

    # opencode-cli upstream mode (local, official CLI, restricted).
    opencode_bin: str = "opencode"
    opencode_models_cmd: str = "opencode models"
    opencode_run_timeout_s: int = 120
    opencode_xdg_data_home: str = ""
    opencode_allowed_model_prefixes: list[str] = field(
        default_factory=lambda: ["opencode/"]
    )
    models_cache_ttl_s: int = 300

    @property
    def dev_open(self) -> bool:
        """When no gateway keys are configured, run in open dev mode."""
        return len(self.gateway_api_keys) == 0

    @property
    def is_opencode_cli(self) -> bool:
        return self.upstream_mode == "opencode-cli"


def load_settings() -> Settings:
    keys_raw = os.environ.get("GATEWAY_API_KEYS", "")
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    mode = os.environ.get("UPSTREAM_MODE", "openai").strip().lower()
    if mode not in UPSTREAM_MODES:
        raise ValueError(
            f"UPSTREAM_MODE must be one of {UPSTREAM_MODES}, got {mode!r}"
        )
    prefixes_raw = os.environ.get("OPENCODE_ALLOWED_MODEL_PREFIXES", "opencode/")
    prefixes = [p.strip() for p in prefixes_raw.split(",") if p.strip()]
    if not prefixes:
        prefixes = ["opencode/"]
    return Settings(
        upstream_mode=mode,
        upstream_base_url=os.environ.get(
            "UPSTREAM_BASE_URL", "https://api.openai.com"
        ).rstrip("/"),
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
        gateway_api_keys=keys,
        port=int(os.environ.get("PORT", "8787")),
        reasoning_passthrough=os.environ.get(
            "REASONING_PASSTHROUGH", "true"
        ).lower()
        in ("1", "true", "yes"),
        requests_per_minute=int(os.environ.get("REQUESTS_PER_MINUTE", "60")),
        opencode_bin=os.environ.get("OPENCODE_BIN", "opencode"),
        opencode_models_cmd=os.environ.get(
            "OPENCODE_MODELS_CMD", "opencode models"
        ),
        opencode_run_timeout_s=int(
            os.environ.get("OPENCODE_RUN_TIMEOUT_S", "120")
        ),
        opencode_xdg_data_home=os.environ.get("OPENCODE_XDG_DATA_HOME", ""),
        opencode_allowed_model_prefixes=prefixes,
        models_cache_ttl_s=int(os.environ.get("MODELS_CACHE_TTL_S", "300")),
    )
