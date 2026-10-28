"""Edge settings: env-only, no dependency on the gateway's ``Settings``.

The edge container never talks to the upstream Zen API or the egress agents —
it only speaks to two OpenAI-compatible bases. Keeping its config separate means
a gateway env change can never brick the public port.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_FALLBACK_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
DEFAULT_PRIMARY_BASE_URL = "http://127.0.0.1:10002"

# Only the OpenAI chat endpoint is translated into a fallback call: the backup
# relay speaks nothing else. /v1/responses and /v1/messages would need a full
# format conversion, so they keep the primary's own error instead.
DEFAULT_FALLBACK_PATHS = "/v1/chat/completions"

_MODES = ("auto", "primary", "fallback")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv_set(name: str, default: str) -> frozenset[str]:
    raw = os.environ.get(name, default)
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


@dataclass(frozen=True, slots=True)
class EdgeSettings:
    """Everything the edge needs, all of it from the environment."""

    # Where the model gateway lives. In compose this is the service name.
    primary_base_url: str = DEFAULT_PRIMARY_BASE_URL
    # Backup relay (proxy_ms). Empty disables failover entirely.
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    # The backup serves ModelScope ids, not the public alias, so the request
    # body is rewritten on the way in.
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    fallback_paths: frozenset[str] = field(
        default_factory=lambda: frozenset({"/v1/chat/completions"})
    )
    # Statuses that mean "the gateway could not serve" rather than "the client
    # asked for something invalid". 4xx client errors are never retried.
    failover_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({502, 503, 504})
    )
    # A 429 that carries a daily-quota verdict means the primary is out of
    # allowance for hours; the backup keeps users working meanwhile.
    failover_on_quota: bool = True
    probe_interval_s: float = 2.0
    probe_timeout_s: float = 2.0
    connect_timeout_s: float = 5.0
    max_body_bytes: int = 32 * 1024 * 1024
    # Empty = admin endpoints disabled (404). Set it to steer routes by hand,
    # e.g. drain the primary before a rebuild.
    admin_key: str = ""
    mode: str = "auto"


def load_edge_settings() -> EdgeSettings:
    mode = os.environ.get("EDGE_MODE", "auto").strip().lower() or "auto"
    if mode not in _MODES:
        raise ValueError(f"EDGE_MODE must be one of {_MODES}, got {mode!r}")
    statuses = _csv_set("EDGE_FAILOVER_STATUSES", "502,503,504")
    return EdgeSettings(
        primary_base_url=(
            os.environ.get("EDGE_PRIMARY_BASE_URL", DEFAULT_PRIMARY_BASE_URL).strip()
            or DEFAULT_PRIMARY_BASE_URL
        ).rstrip("/"),
        fallback_base_url=os.environ.get("EDGE_FALLBACK_BASE_URL", "").strip().rstrip("/"),
        fallback_api_key=os.environ.get("EDGE_FALLBACK_API_KEY", "").strip(),
        fallback_model=(
            os.environ.get("EDGE_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip()
            or DEFAULT_FALLBACK_MODEL
        ),
        fallback_paths=_csv_set("EDGE_FALLBACK_PATHS", DEFAULT_FALLBACK_PATHS),
        failover_statuses=frozenset(int(s) for s in statuses if s.isdigit()),
        failover_on_quota=_env_bool("EDGE_FAILOVER_ON_QUOTA", True),
        probe_interval_s=max(0.2, _env_float("EDGE_PROBE_INTERVAL_S", 2.0)),
        probe_timeout_s=max(0.2, _env_float("EDGE_PROBE_TIMEOUT_S", 2.0)),
        connect_timeout_s=max(0.2, _env_float("EDGE_CONNECT_TIMEOUT_S", 5.0)),
        max_body_bytes=max(1024, _env_int("EDGE_MAX_BODY_BYTES", 32 * 1024 * 1024)),
        admin_key=os.environ.get("EDGE_ADMIN_KEY", "").strip(),
        mode=mode,
    )


__all__ = [
    "DEFAULT_FALLBACK_MODEL",
    "DEFAULT_FALLBACK_PATHS",
    "DEFAULT_PRIMARY_BASE_URL",
    "EdgeSettings",
    "load_edge_settings",
]
