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

# Model id namespaces differ per mode:
#   - zen-direct talks to opencode.ai, whose catalogue switched to BARE ids
#     on 2026-10-02 ("big-pickle"); the historical "opencode/big-pickle"
#     spelling now answers 401 ModelError ("Model ... is not supported").
#   - opencode-serve drives a local `opencode serve`, which still uses the
#     opencode-internal "provider/model" ids.
DEFAULT_SERVE_MODELS = ("opencode/big-pickle",)
DEFAULT_ZEN_MODELS = ("big-pickle",)

# Public-facing model catalogue. Entries are "id:display_name:upstream".
# Users only ever see `id`; upstream is resolved server-side and scrubbed
# from every response (JSON bodies and streamed SSE chunks alike).
# The default upstream must stay in sync with the live zen catalogue
# (GET https://opencode.ai/zen/v1/models) — see the 2026-10-02 drift note
# in CHANGELOG.md.
DEFAULT_PUBLIC_MODELS = ("ds41f_cus:DeepSeek V4.1 Flash:big-pickle",)

# Admin API keys: permanent, can manage the dynamic key store via /admin/*.
# This is a clearly-fake LOCAL DEV placeholder, never a real credential (see
# SECURITY.md: no key may ever be committed). Production deployments MUST set
# ADMIN_API_KEYS explicitly; otherwise anyone who reads this repo knows the
# admin key of every gateway that kept the default.
DEFAULT_ADMIN_API_KEYS = ("dev-admin-key",)


def _default_public_models() -> list[dict[str, str]]:
    """Dataclass default factory — same parsing rules as PUBLIC_MODELS.

    Delegates to `_parse_public_models` so a built-in display name containing
    a colon is split exactly like an env-supplied one (the old `split(":", 2)`
    disagreed with the env path and silently moved the colon into `upstream`).
    """
    return _parse_public_models(",".join(DEFAULT_PUBLIC_MODELS))

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
    # Permanent admin keys (manage /admin/keys; also valid for chat calls).
    admin_api_keys: list[str] = field(
        default_factory=lambda: list(DEFAULT_ADMIN_API_KEYS)
    )
    # Dynamic key store (JSON file). Keys created via POST /admin/keys live
    # here with an expiry timestamp; static GATEWAY_API_KEYS stay permanent.
    key_store_path: str = "keys.json"
    key_default_ttl_hours: float = 24.0
    # Model masking: when True, /v1/models lists only the public catalogue
    # and every response's model field is rewritten to the public id.
    mask_models: bool = True
    public_models: list[dict[str, str]] = field(
        default_factory=_default_public_models
    )
    # Bind address. Loopback by default; set HOST=0.0.0.0 to serve the LAN
    # (requires GATEWAY_API_KEYS — enforced in load_settings).
    host: str = "127.0.0.1"
    port: int = 8787
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
    # Free-tier gate fingerprint (re-verified 2026-09-30 by live ablation):
    # the gate requires opencode's builtin tool schemas in the body but NO
    # LONGER checks the default system prompt (a one-line or missing system
    # message passes; tools-only 403s). Marker injection is therefore
    # configurable:
    #   "bridge" (default) — prepend a tiny system note steering the model
    #                        away from the injected builtin tools (~70 tokens)
    #   "full"             — legacy: prepend opencode's default prompt +
    #                        bridge note (~7.8k tokens; pre-gate-change
    #                        behaviour, kept as a per-bucket fallback)
    #   "none"             — no system message injected at all
    zen_marker_mode: str = "bridge"
    # Which builtin tool schemas to inject for the free-tier gate.
    # Live ablation 2026-09-30: the gate requires >= 2 tools whose NAMES are
    # opencode builtin names and ignores their schema content entirely.
    #   "minimal"   (default) — two synthesized ~30-token schemas (~60 prompt
    #                           tokens total; was ~1,970 with the captured set)
    #   "captured2"           — the {bash, read} subset of the captured asset
    #                           (~2k tokens; intermediate fallback)
    #   "all"                 — the full captured 11-tool schema list
    #                           (~6k tokens; legacy, per-bucket fallback)
    zen_tools_mode: str = "minimal"
    # Debug: dump every raw client request body to this directory (one JSON
    # file per request, oldest pruned beyond 200 files). Empty -> disabled.
    # Bodies are dumped BEFORE whitelist filtering, so client-side prompt
    # bloat (claude code tool schemas, hermes system prompt, ...) can be
    # audited offline for token waste. Covers all three protocol routes.
    payload_dump_dir: str = ""
    # Empty -> anonymous free tier ("Bearer public" + system-prompt marker).
    zen_api_key: str = ""
    zen_models: list[str] = field(default_factory=lambda: list(DEFAULT_ZEN_MODELS))
    zen_timeout_s: int = 300
    # Optional egress proxy for the zen client, e.g. "http://127.0.0.1:7897".
    zen_proxy: str = ""
    # Whether the httpx clients may follow ambient proxy settings
    # (HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY, plus the Windows
    # registry proxy). Applies to every outbound client: zen-direct, the
    # generic openai passthrough adapter and the balancer.
    #
    # Default FALSE. Measured 2026-10-01: the win10-local bucket inherited a
    # stale HTTP_PROXY pointing at a dead Clash instance, so every upstream
    # call failed with ConnectError in ~2.0s and the bucket answered 502 while
    # its network was perfectly healthy. Coupling the egress path to ambient
    # machine state is fragile; route explicitly with ZEN_PROXY instead.
    upstream_trust_env: bool = False
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


def _parse_public_models(value: str) -> list[dict[str, str]]:
    """Parse "id:display_name:upstream" entries into catalogue dicts.

    `id` never contains a colon and `upstream` is a "provider/model" pair that
    never contains one either, so the display name is allowed to (everything
    between the first and last segment). Malformed entries are skipped rather
    than silently dropping the whole catalogue.
    """
    out: list[dict[str, str]] = []
    for part in _csv(value):
        pieces = part.split(":")
        if len(pieces) < 3:
            continue
        mid = pieces[0].strip()
        upstream = pieces[-1].strip()
        display = ":".join(pieces[1:-1]).strip()
        if not (mid and upstream):
            continue
        out.append({"id": mid, "display_name": display, "upstream": upstream})
    return out


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var; unset OR empty means "use the default".

    Treating an empty value as False was a footgun: `set MASK_MODELS=` in a
    .cmd/.env file silently turned model masking off, leaking upstream model
    names. Write an explicit `false`/`0`/`no` to disable a flag.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    admin_api_keys = _csv(os.environ.get("ADMIN_API_KEYS", ""))
    if host not in LOOPBACK_HOSTS:
        if not gateway_api_keys:
            raise ValueError(
                f"Binding a non-loopback host ({host!r}) without GATEWAY_API_KEYS "
                "would expose an unauthenticated OpenAI-compatible proxy to the "
                "network; set GATEWAY_API_KEYS first"
            )
        # The built-in admin default is a *published* local-dev placeholder
        # (this repo is public), so it must never guard a network-exposed
        # gateway: anyone could then mint API keys. Loopback keeps the
        # convenience default; anything else must be configured explicitly.
        if not admin_api_keys:
            raise ValueError(
                f"Binding a non-loopback host ({host!r}) without ADMIN_API_KEYS "
                "would leave the built-in placeholder ('dev-admin-key') as the "
                "admin key, and that value is public. Set ADMIN_API_KEYS "
                "explicitly (or bind 127.0.0.1 for local development)."
            )
    public_models = _parse_public_models(
        os.environ.get(
            "PUBLIC_MODELS", ",".join(DEFAULT_PUBLIC_MODELS)
        )
    )
    if not public_models:
        public_models = _parse_public_models(",".join(DEFAULT_PUBLIC_MODELS))
    zen_marker_mode = os.environ.get("ZEN_MARKER_MODE", "bridge").strip().lower()
    if zen_marker_mode not in ("full", "bridge", "none"):
        raise ValueError(
            "ZEN_MARKER_MODE must be one of full|bridge|none, "
            f"got {zen_marker_mode!r}"
        )
    zen_tools_mode = os.environ.get("ZEN_TOOLS_MODE", "minimal").strip().lower()
    if zen_tools_mode not in ("minimal", "captured2", "all"):
        raise ValueError(
            "ZEN_TOOLS_MODE must be one of minimal|captured2|all, "
            f"got {zen_tools_mode!r}"
        )
    return Settings(
        upstream_mode=mode,
        upstream_base_url=os.environ.get(
            "UPSTREAM_BASE_URL", "https://api.openai.com"
        ).rstrip("/"),
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
        gateway_api_keys=gateway_api_keys,
        admin_api_keys=admin_api_keys or list(DEFAULT_ADMIN_API_KEYS),
        key_store_path=os.environ.get("KEY_STORE_PATH", "keys.json"),
        key_default_ttl_hours=float(os.environ.get("KEY_DEFAULT_TTL_HOURS", "24")),
        mask_models=_env_bool("MASK_MODELS", True),
        public_models=public_models,
        host=host,
        port=int(os.environ.get("PORT", "8787")),
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
        zen_marker_mode=zen_marker_mode,
        zen_tools_mode=zen_tools_mode,
        payload_dump_dir=os.environ.get("PAYLOAD_DUMP_DIR", "").strip(),
        zen_api_key=os.environ.get("OPENCODE_ZEN_API_KEY", ""),
        zen_models=_csv(
            os.environ.get("OPENCODE_ZEN_MODELS", ",".join(DEFAULT_ZEN_MODELS))
        )
        or list(DEFAULT_ZEN_MODELS),
        zen_timeout_s=int(os.environ.get("OPENCODE_ZEN_TIMEOUT_S", "300")),
        zen_proxy=os.environ.get("ZEN_PROXY", "").strip(),
        upstream_trust_env=_env_bool("UPSTREAM_TRUST_ENV", False),
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
