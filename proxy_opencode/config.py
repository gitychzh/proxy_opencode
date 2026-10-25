"""Configuration from environment variables.

Single source of truth for all env knobs. Keep names stable; document every
one in README.md.
"""

from __future__ import annotations

import os
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
# from every response (JSON bodies and streamed SSE chunks alike). The
# standard /v1/models body is plain OpenAI shape (id/object/created/
# owned_by); `display_name` stays internal. The default upstream must stay
# in sync with the live zen catalogue (GET
# https://opencode.ai/zen/v1/models) — see the 2026-10-02 drift note in
# CHANGELOG.md.
DEFAULT_PUBLIC_MODELS = (
    # 2026-10-16: the alias says what the model is (big-pickle IS DeepSeek
    # V4.1 Flash upstream). 2026-10-17: big-pickle is the ONLY relayed
    # upstream model — its raw id (and every other free id) is neither
    # listable nor callable through the standard paths.
    "deepseek-ai/deepseek-v4.1-flash:DeepSeek V4.1 Flash:big-pickle",
)

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
DEFAULT_ZEN_CLIENT_VERSION = "1.19.4"
DEFAULT_ZEN_BUN_VERSION = "1.4.4"


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
    # Free-tier body gate (round 5, re-verified 2026-10-25 by live ablation
    # on a fresh egress IP): in addition to the tool-name shape gate, the
    # upstream requires the CURRENT genuine opencode system prompt in the
    # body — a tiny bridge note (or any small system message) yields the
    # layer-2-shaped 429 FreeUsageLimitError even with a fresh session,
    # bun transport and genuine tool schemas. Tool schemas and the user
    # message content/length stay irrelevant (4 synthetic schemas + a
    # one-line user message pass, once the genuine prompt is present).
    # Marker injection is therefore configurable:
    #   "full" (default)  — prepend opencode's genuine default prompt
    #                       (zen_prompt_default.txt asset) + bridge note;
    #                       the only mode proven to pass the body gate
    #   "bridge"          — tiny system note only (~70 tokens); FAILS the
    #                       body gate since 2026-10-24 (429 masquerade)
    #   "none"            — no system message injected at all; also fails
    zen_marker_mode: str = "full"
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
    # The relay allowlist: the ONLY upstream models this gateway fronts.
    # /v1/models_cus lists it, and the unmasked list_models fallback uses it.
    # Clients address models by public alias (PUBLIC_MODELS), never by these
    # raw ids.
    zen_models: list[str] = field(default_factory=lambda: list(DEFAULT_ZEN_MODELS))
    # Requested model id -> upstream bare id rewrite (round 5, 2026-10-25):
    # the anonymous free tier only serves the ``-free`` suffixed ids; a paid
    # bare id (e.g. deepseek-v4.1-flash) answers 401 AuthError "Missing API
    # key" even with an otherwise perfect body. Map public aliases to free
    # ids here, e.g. ZEN_MODEL_MAP=deepseek-ai/deepseek-v4.1-flash=deepseek-v4.1-flash-free
    zen_model_map: dict[str, str] = field(default_factory=dict)
    zen_timeout_s: int = 300
    # Stream stall watchdog: max tolerated silence between SSE body bytes
    # (covering both the first byte after the response headers and gaps
    # mid-stream) before StreamRelay stops waiting, synthesises a terminal
    # finish_reason:"length" chunk + [DONE] and releases the egress lease
    # as a failure (kind=stream_stalled -> three consecutive failures
    # cordon the bucket). Measured 2026-10-19: a dead egress tunnel held a
    # stream silent for the full 300s zen_timeout_s read timeout, the
    # resulting httpx.ReadTimeout tore the client connection down with no
    # terminator and hermes reported stream_drop; its own silent-stream
    # watchdog fires at 45s, so the default stays well below 300s.
    # 0 disables the watchdog (legacy behaviour).
    stream_stall_s: float = 60
    # Client-write stall watchdog: max seconds the consumer may go without
    # pulling the next relayed chunk while `relay_stream` is parked at its
    # yield (that state == the ASGI server is blocked writing that chunk to
    # the client: uvicorn drains under backpressure). Measured 2026-10-19:
    # a blackholed home-broadband leg left hermes receiving nothing for
    # 600.5s while upstream kept producing — the gateway held the stream
    # open until hermes killed it; with the watchdog it logs a WARNING
    # (`client write stalled`), ends the stream as `client_stalled` and
    # closes the client connection after ~client_stall_s instead, so the
    # client fails over in seconds rather than minutes. 0 disables.
    client_stall_s: float = 60
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
    # LEGACY (kept for env compatibility with openai-serve-era deployments):
    # the 1.19.4 client no longer sends an `x-session-id` header at all,
    # so the gateway does not either. Reading this env has no effect since
    # 0.7.6.
    zen_session_id: str = ""
    # 2026-10-24 mirror note: the live 1.19.4 client no longer sends
    # `x-session-id` at all; it sends a 40-hex workspace hash in
    # `x-opencode-project`. Empty -> a fixed instance-level hash is used;
    # set OPENCODE_ZEN_PROJECT_ID to pin a specific captured value.
    zen_project_id: str = ""
    zen_client_version: str = DEFAULT_ZEN_CLIENT_VERSION
    # bun transport (2026-10-24 stack gate): the anonymous free tier judges
    # the TLS/HTTP transport fingerprint — byte-exact httpx replays of a
    # genuine client capture got the quota-shaped 429 from fresh IPs while
    # bun (boringssl, HTTP/2) replays of the same bytes got 200. When
    # enabled, the zen-direct adapter drives one short-lived `bun
    # forward.ts` subprocess per attempt instead of httpx (SSE passthrough;
    # egress pool / failover semantics unchanged). Off by default so tests
    # and no-bun deployments keep the httpx fallback; the gateway image
    # ships the bun binary and sets ZEN_BUN_TRANSPORT=1.
    zen_bun_transport: bool = False
    zen_bun_bin: str = "/usr/local/bin/bun"
    # Empty -> <repo>/proxy_opencode/zen_bun/forward.ts (the bundled script).
    zen_bun_script: str = ""
    zen_bun_version: str = DEFAULT_ZEN_BUN_VERSION
    # 0.7.x egress pool: per-bucket device agents. Format:
    #   ZEN_EGRESS_PROXIES="name=http://<device>:10001,name2=http://<device>:10002"
    # Optional bucket-identity prefixes (an agent reporting an IP outside the
    # prefix is treated as unhealthy — it would silently consume another
    # bucket's quota):
    #   ZEN_EGRESS_EXPECT_PREFIX="opc2-wifi=117.95.231.0/24,..."
    zen_egress_proxies: dict[str, str] = field(default_factory=dict)
    zen_egress_expect_prefix: dict[str, str] = field(default_factory=dict)
    # Shared secret for the agents' /healthz + /rotate endpoints (basic auth).
    zen_egress_secret: str = ""
    zen_egress_probe_interval_s: int = 30
    zen_egress_cordon_s: int = 300
    # P5 IP rotation: per-bucket daily request threshold. Only listed
    # buckets rotate (those whose agent has an ONT configured); empty =
    # the whole rotation feature is off.
    #   ZEN_EGRESS_ROTATION="opc2-eth=500,opc2-wifi=500,cc9=500"
    zen_egress_rotation: dict[str, int] = field(default_factory=dict)
    # Counters/rotation JSON snapshot for restart survival (container has
    # a /data volume for exactly this); empty = in-memory only.
    #   ZEN_EGRESS_COUNTS_PATH="/data/egress_counts.json"
    zen_egress_counts_path: str = ""
    # Structured JSONL event sink for the whole egress layer (cordons, 429s,
    # probe IP changes, rotations with before/after IP). Empty = off; the
    # container mounts /data for exactly this.
    #   ZEN_EGRESS_EVENT_LOG="/data/egress_events.jsonl"
    zen_egress_event_log: str = ""

    # Backup chain failover (2026-10-25): when the zen-direct chain fails at
    # the egress level (quota 429 / 5xx / transport), serve chat through a
    # backup OpenAI-compatible gateway (proxy_ms, ModelScope aggregation,
    # same ECS host) and switch back automatically. Empty base URL disables
    # the feature entirely (wrapper never engages; the adapter is the bare
    # primary, so tests and existing deployments see zero behaviour change).
    #   ZEN_FALLBACK_BASE_URL=http://172.17.0.1:10003/v1
    #   ZEN_FALLBACK_API_KEY=<proxy_ms CLIENT_KEYS value>
    #   ZEN_FALLBACK_HOLD_S=120        (breaker hold window after a trip)
    #   ZEN_FALLBACK_MODEL_MAP=deepseek-ai/deepseek-v4.1-flash=deepseek-ai/DeepSeek-V4.1-Flash-1017
    zen_fallback_base_url: str = ""
    zen_fallback_api_key: str = ""
    zen_fallback_hold_s: float = 120.0
    zen_fallback_model_map: dict[str, str] = field(default_factory=dict)
    # Catch-all for ids the map misses: the zen catalogue's bare ids can never
    # exist on the backup gateway, so an unmapped id would be rejected there.
    # NOTE the wrapper receives ids AFTER the route layer's alias resolution
    # (the public alias deepseek-ai/deepseek-v4.1-flash arrives as big-pickle),
    # so map keys must be the resolved upstream ids, not the public ones.
    zen_fallback_default_model: str = ""

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


def _model_map(value: str) -> dict[str, str]:
    """Parse ``ZEN_MODEL_MAP`` entries of the form ``from=to`` (csv)."""
    mapping: dict[str, str] = {}
    for part in _csv(value):
        src, sep, dst = part.partition("=")
        if sep and src and dst:
            mapping[src] = dst
    return mapping


def _parse_kv(value: str) -> dict[str, str]:
    """Parse "name=url,name2=url2" into a dict (egress pool specs)."""

    out: dict[str, str] = {}
    for part in _csv(value):
        key, sep, item = part.partition("=")
        if not sep or not key.strip() or not item.strip():
            raise ValueError(
                f"bad key=value entry {part!r} (expected 'name=url,...')"
            )
        out[key.strip()] = item.strip()
    return out


def _parse_kv_int(value: str) -> dict[str, int]:
    """Parse "name=500,..." into an int dict (rotation thresholds)."""

    out: dict[str, int] = {}
    for part in _csv(value):
        key, sep, item = part.partition("=")
        try:
            num = int(item) if sep else 0
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"bad integer entry {part!r} (expected 'name=500,...')"
            ) from exc
        if not sep or not key.strip() or num <= 0:
            raise ValueError(
                f"bad integer entry {part!r} (expected 'name=<positive int>,...')"
            )
        out[key.strip()] = num
    return out


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
    zen_marker_mode = os.environ.get("ZEN_MARKER_MODE", "full").strip().lower()
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
        zen_model_map=_model_map(os.environ.get("ZEN_MODEL_MAP", "")),
        zen_timeout_s=int(os.environ.get("OPENCODE_ZEN_TIMEOUT_S", "300")),
        stream_stall_s=float(os.environ.get("STREAM_STALL_S", "60")),
        client_stall_s=float(os.environ.get("CLIENT_STALL_S", "60")),
        zen_proxy=os.environ.get("ZEN_PROXY", "").strip(),
        upstream_trust_env=_env_bool("UPSTREAM_TRUST_ENV", False),
        zen_session_id=os.environ.get("OPENCODE_ZEN_SESSION_ID", "").strip(),
        zen_client_version=os.environ.get(
            "OPENCODE_ZEN_CLIENT_VERSION", DEFAULT_ZEN_CLIENT_VERSION
        ),
        zen_bun_transport=_env_bool("ZEN_BUN_TRANSPORT", False),
        zen_bun_bin=os.environ.get("ZEN_BUN_BIN", "/usr/local/bin/bun").strip(),
        zen_bun_script=os.environ.get("ZEN_BUN_SCRIPT", "").strip(),
        zen_bun_version=os.environ.get(
            "OPENCODE_ZEN_BUN_VERSION", DEFAULT_ZEN_BUN_VERSION
        ),
        zen_project_id=os.environ.get("OPENCODE_ZEN_PROJECT_ID", "").strip(),
        zen_egress_proxies=_parse_kv(os.environ.get("ZEN_EGRESS_PROXIES", "")),
        zen_egress_expect_prefix=_parse_kv(
            os.environ.get("ZEN_EGRESS_EXPECT_PREFIX", "")
        ),
        zen_egress_secret=os.environ.get("ZEN_EGRESS_SECRET", "").strip(),
        zen_egress_probe_interval_s=int(
            os.environ.get("ZEN_EGRESS_PROBE_INTERVAL_S", "30")
        ),
        zen_egress_cordon_s=int(os.environ.get("ZEN_EGRESS_CORDON_S", "300")),
        zen_egress_rotation=_parse_kv_int(os.environ.get("ZEN_EGRESS_ROTATION", "")),
        zen_egress_counts_path=os.environ.get(
            "ZEN_EGRESS_COUNTS_PATH", ""
        ).strip(),
        zen_egress_event_log=os.environ.get(
            "ZEN_EGRESS_EVENT_LOG", ""
        ).strip(),
        zen_fallback_base_url=os.environ.get("ZEN_FALLBACK_BASE_URL", "").strip(),
        zen_fallback_api_key=os.environ.get("ZEN_FALLBACK_API_KEY", "").strip(),
        zen_fallback_hold_s=float(os.environ.get("ZEN_FALLBACK_HOLD_S", "120")),
        zen_fallback_model_map=_model_map(
            os.environ.get("ZEN_FALLBACK_MODEL_MAP", "")
        ),
        zen_fallback_default_model=os.environ.get(
            "ZEN_FALLBACK_DEFAULT_MODEL", ""
        ).strip(),
    )
