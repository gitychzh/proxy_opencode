"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    upstream_base_url: str = "https://api.openai.com"
    upstream_api_key: str = ""
    gateway_api_keys: list[str] = field(default_factory=list)
    port: int = 8787
    reasoning_passthrough: bool = True
    requests_per_minute: int = 60

    @property
    def dev_open(self) -> bool:
        """When no gateway keys are configured, run in open dev mode."""
        return len(self.gateway_api_keys) == 0


def load_settings() -> Settings:
    keys_raw = os.environ.get("GATEWAY_API_KEYS", "")
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    return Settings(
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
    )
