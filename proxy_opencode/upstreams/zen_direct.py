"""zen-direct adapter: talks to OpenCode Zen's OpenAI-compatible API directly.

UPSTREAM_MODE=zen-direct reconstructs the client protocol the official
opencode 1.19.x client sends to `https://opencode.ai` (the inference
service; endpoint shape mirrored from HEAD captures of a genuine client):

  * Endpoint: POST {base}/chat/completions (SSE streaming supported).
  * Auth: `Authorization: Bearer public` for the anonymous free tier, or a
    real Zen API key when OPENCODE_ZEN_API_KEY is set.
  * Protocol headers (verified byte-for-byte against a capture of the
    genuine 1.19.4 CLI, 2026-10-24):
        Authorization: Bearer public
        Content-Type: application/json
        User-Agent: opencode/<ver> ai-sdk/provider-utils/<pv> runtime/bun/<bv>
        x-opencode-client: cli
        x-opencode-project: <40-hex workspace hash>
        x-opencode-session: ses_<26 chars>     (descending-minted)
        x-opencode-session-id: ses_<26 chars>  (same value, second header)
        x-opencode-request: msg_<26 chars>     (ascending-minted)
  * Identifiers replicate opencode's `packages/schema/src/identifier.ts`:
    26 chars = 12 hex chars of `(ms << 12) + counter` (low 48 bits) followed
    by 14 random chars from [0-9A-Za-z].

  * Anonymous free tier is a TWO-LAYER gate (live ablations 2026-10-24,
    byte-clone replays of a genuine-1.19.4 request from a clean residential
    egress):

      Layer 1 — shape gate: HTTP 403 `FreeTierError`
        - requires **>= 4 DISTINCT tools whose NAMES are opencode builtin
          names** (1/2/3 real names all 403; 4 real names 200 with EMPTY
          schemas; a renamed/unknown name 403s even amid 11 real ones).
        - everything else is NOT checked: UA version (2.0.18 UA passed),
          schema/description contents, `x-opencode-client`, the exact
          session-header names (2.0-style renames passed), and the b3/
          traceparent tracing headers are all tolerated or unnecessary.
        - gate verdict arrives BEFORE the quota layer: a shape-faulted
          request 403s even from an IP whose daily quota is exhausted.
        - HISTORY: through 2026-10-23 the minimum was 2 real names with
          synthetic schemas accepted; with the 2026-10-24 upstream the
          count tightened to 4 real names (see _MIN_INJECTED_TOOLS).

      Layer 2 — quota gate: HTTP 429 `FreeUsageLimitError`
        - metered per EGRESS IP per UTC day; a fresh 200-serving IP can
          exhaust its daily allowance within ~a few dozen successful
          calls, and `Retry-After` points at the next UTC midnight
          (measured win10 home egress, 2026-10-24).
        - transfer failures / TLS resets / 403s do NOT burn quota.
        - TRANSPORT-STACK CAVEAT (live cross-check 2026-10-24): the quota
          verdict is NOT purely per-IP. A byte-exact genuine-client
          capture replayed by httpx got 429 from a FRESH egress IP while
          bun replays (boringssl, HTTP/2) of the same bytes got 200 from
          the SAME IP minutes later; freshly ONT-rotated IPs kept 429ing
          under httpx across the pool. The upstream judges the TLS/HTTP
          fingerprint and answers non-bun stacks with the quota-shaped
          429. The bun subprocess transport (zen_bun) restores wire
          parity; httpx remains the dev/tests fallback.

    Consequence for the pool: a batch of troubleshooting probes from a
    bucket's egress IP can drain that IP's own daily allowance (the
    2026-10-24 whole-pool 429 outage was exactly this); the 403 shape gate
    must be fixed at the gateway level and never masked as an IP problem.

Everything else is a faithful OpenAI passthrough: messages/params are
forwarded via the shared whitelist, SSE chunks are relayed as-is, and
upstream errors are relayed with their original status code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote_plus, urlparse

import httpx

from ..config import Settings
from ..egress.pool import EgressPool, Lease
from .openai_http import ResponseLike, StreamRelay, _raw_relay_body
from .zen_bun_transport import BunTransport, BunTransportError

logger = logging.getLogger("proxy_opencode.zen")

_DIR = Path(__file__).parent
_ASSET_PROMPT = _DIR / "zen_prompt_default.txt"
_ASSET_TOOLS = _DIR / "zen_builtin_tools.json"

# Appended after the marker prompt so the model serves the *client's* tools
# instead of opencode's built-ins. Kept separate from the marker text.
_BRIDGE_NOTE = (
    "\n\n---\n"
    "The system prompt and tool definitions above establish the opencode "
    "runtime context required by the upstream provider. You are now serving "
    "an OpenAI-compatible API client: answer the client's latest message "
    "directly, using ONLY the additional client-supplied tools appended "
    "after the opencode built-ins (if any). NEVER call the opencode built-in "
    "tools (bash, read, edit, glob, grep, write, list, task, todowrite, "
    "webfetch, websearch, skill) — they are unavailable in this session; "
    "calling them fails. Reply in the language of the client's latest "
    "message."
)

# Standalone tiny system note for marker_mode="bridge" (~70 tokens): the
# built-in tool names are still injected for the observed gate, but the full
# captured prompt is not. The note steers the model away from calling them.
_BRIDGE_NOTE_STANDALONE = (
    "You are serving an OpenAI-compatible API client. Answer the client's "
    "latest message directly, using ONLY the client-supplied tools (if any). "
    "NEVER call the opencode built-in tools (bash, read, edit, glob, grep, "
    "write, list, task, todowrite, webfetch, websearch, skill) - they are "
    "unavailable in this session; calling them fails. Reply in the language "
    "of the client's latest message."
)

# Minimal builtin schema set that still satisfies the shape gate.
#
# Live ablation 2026-10-24 (byte-clone replays of a genuine 1.19.4 request
# from a clean residential egress) established the CURRENT rule set — the
# gate TIGHTENED from the 2026-09-30 era where 2 real names passed:
#
#   11 real captured tools (full schemas)         -> 200
#   bash+edit+glob+read, empty desc/params (hollow) -> 200
#   bash+edit+glob+read, real schemas             -> 200
#   bash+edit+glob   (3 real)                     -> 403
#   bash+edit        (2 real)                     -> 403
#   bash alone       (1 real)                     -> 403
#   11 real names + 1 renamed (tool_0)            -> 403
#   2 synthetic schemas under real names (old era)  -> 403  (count < 4)
#
# So: >= 4 DISTINCT builtin names, schemas/descriptions NOT inspected.
# `_MIN_INJECTED_TOOLS` sends 4 synthesized ~30-token schemas under real
# names (~120 prompt tokens total); `captured2` injects the captured asset
# subset below and `all` the full 11-tool list as fallbacks if the gate
# ever changes again.
#
# A SECOND, independent quota gate appeared with the 2026-10-24 study as
# the per-egress-IP/UTC-day allowance (FreeUsageLimitError + Retry-After
# to UTC midnight) — separate from this shape gate; see the module
# docstring.
_MINIMAL_TOOL_NAMES = ("bash", "edit", "glob", "read")

_MIN_INJECTED_TOOLS = 4

# Bucket-failover cap for 429/5xx/403-free-tier/transport retries across egress
# buckets (0.7.x). A fully-drained pool must still surface its last upstream
# error instead of looping until the client times out; capped overhead is 2
# extra attempts in the worst case.
_FAILOVER_MAX_ATTEMPTS = 3

# The shape gate's rejection type. Measured 2026-10-24 on a clean
# residential egress (Home win-win, alive quota window): requests carrying
# < 4 real builtin tool names get 403 FreeTierError from a healthy egress
# — a REQUEST-level shape failure. It is kept in the egress-failure path
# below as well (cordons + failover + 503 rewrite) because the two gates
# cannot be told apart for certain from one answer alone, and a spurious
# cordon on a shape fault costs nothing: the rotator lands a fresh IP and
# the next request with the corrected shape re-opens the bucket. A quota
# 429, by contrast, is pure egress level. (2026-10-17's "same body, device
# IPs 403 / fresh IP 200" is now explained as shape-vs-clean-egress mixed
# windows, not a per-IP shape flag.)
_FREE_TIER_ERROR = "FreeTierError"


def _is_free_tier_rejection(status: Any, content: Any) -> bool:
    """True when an error body is the free tier's egress-IP gate rejection."""

    if status != 403 or not isinstance(content, dict):
        return False
    err = content.get("error")
    return isinstance(err, dict) and err.get("type") == _FREE_TIER_ERROR


# Client-facing answer when a free-tier 403 is the FINAL outcome (failover
# budget spent, or no pool to move to). Kept as a defensive rewrite from
# 0.7.5 (2026-10-19): a relayed 403 makes clients blame their credentials and
# stop retrying — measured then, when the shape gate was still misdiagnosed
# and two IPs plus a dead tunnel spent all three attempts. With the
# 2026-10-24 toolshape fix the branch should stay cold.
_EGRESS_UNAVAILABLE_MESSAGE = (
    "Upstream free-tier gate rejected the gateway's egress IP "
    "(403 FreeTierError): an egress-level restriction, not a problem with "
    "this request or your credentials. Try again later."
)


def _egress_unavailable_result() -> dict[str, Any]:
    """Shape a final free-tier 403 as a retryable 503 egress outage."""

    return {
        "__status__": 503,
        "content": {
            "error": {
                "message": _EGRESS_UNAVAILABLE_MESSAGE,
                "type": "api_error",
                "param": None,
                "code": "egress_unavailable",
            }
        },
    }


# Injected names, in preference order. A name the client already uses is
# skipped: injecting a duplicate would not add a distinct builtin name (the
# gate counts names, not entries) and dropping the builtin instead could
# push the count below the four-name minimum. The first four names are the
# ones the live 1.19.4 client itself sends (ablation 2026-10-24):
# bash, edit, glob, read.
_INJECT_NAME_POOL = (
    "bash", "edit", "glob", "read", "todowrite", "skill", "task",
    "list", "webfetch", "websearch", "grep", "write",
)


def _minimal_schema(name: str) -> dict[str, Any]:
    """Smallest structurally valid function-tool schema for `name`."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _default_prompt() -> str:
    return _ASSET_PROMPT.read_text(encoding="utf-8")


def _builtin_tools() -> list[dict[str, Any]]:
    data = json.loads(_ASSET_TOOLS.read_text(encoding="utf-8"))
    return [t for t in data if isinstance(t, dict)]


def _tool_names(tools: list[Any]) -> set[str]:
    names: set[str] = set()
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                names.add(str(fn["name"]))
            elif t.get("name"):
                names.add(str(t["name"]))
    return names


def opencode_id(prefix: str, *, descending: bool = False) -> str:
    """Generate an identifier exactly like opencode's Identifier.create().

    26 chars: 12 hex chars encoding `(ms << 12) + counter` truncated to the
    low 48 bits (bitwise-inverted when `descending`, which is how opencode
    mints SESSION ids since 1.19.0 — see schema/src/session-id.ts), followed
    by 14 random chars drawn from [0-9A-Za-z] (byte % 62).

    Gate note (2026-10-13, live-verified): the free tier rejects a session
    id that is ascending-encoded OR whose counter reaches 64 within the same
    millisecond with `FreeTierError` ("can only be used from within
    OpenCode"). Counter 1 is what a real client emits for the first id in a
    millisecond, so always mint with counter 1.
    """
    now_ms = time.time_ns() // 1_000_000
    current = now_ms * 0x1000 + 1
    value = (~current) & 0xFFFFFFFFFFFF if descending else current & 0xFFFFFFFFFFFF
    time_part = format(value, "012x")
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    rand = "".join(alphabet[b % 62] for b in secrets.token_bytes(14))
    return prefix + time_part + rand


def _project_hash() -> str:
    """Fixed 40-hex workspace hash for `x-opencode-project`.

    The genuine client derives it from the workspace directory (sha1). The
    gateway has no single workspace, so a backend-stable constant keeps the
    header byte-stable without pretending to be one real directory.
    """

    return hashlib.sha1(b"opencode-gateway-workspace").hexdigest()


def is_valid_opencode_id(value: str, prefix: str) -> bool:
    body = value[len(prefix):]
    if len(body) != 26:
        return False
    try:
        int(body[:12], 16)
    except ValueError:
        return False
    return all(c.isalnum() for c in body[12:])


def _clean_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Keep only OpenAI-core per-message fields (upstream rejects extras).

    A message left with neither `content` nor `tool_calls` (e.g. an assistant
    turn whose only part was internal reasoning) is dropped: the upstream
    rejects such turns with HTTP 400 and would fail the whole request.
    """
    keep = ("role", "content", "tool_calls", "tool_call_id", "name")
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        cleaned = {k: v for k, v in m.items() if k in keep and v is not None}
        if "content" not in cleaned and not cleaned.get("tool_calls"):
            continue
        out.append(cleaned)
    return out


def _retry_after_seconds(
    headers: "Mapping[str, str] | httpx.Headers | None",
) -> float | None:
    """Parse an upstream 429's Retry-After into seconds.

    RFC 7231 allows integer seconds or an HTTP-date. The value feeds the
    P5 rotation trigger's measured classification: > 3600s means the
    per-IP daily quota is exhausted (short values are soft throttling).
    Unparseable/absent → None (treated as "no classification available").
    """

    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _log_failover(*, kind: str, remaining: int) -> None:
    """Record one upstream failure handed to the failover loop.

    Counts live in the message body (they are not whitelisted extras);
    never any request content.
    """

    logger.warning(
        f"egress failover: upstream {kind} on one bucket, "
        f"retrying on another ({remaining} attempt(s) left)"
    )


# StreamRelay close-hook outcome -> (pool release ok, kind).
#
# Only transport-level failures count against the egress bucket: a stalled
# or interrupted stream means the tunnel/upstream path stopped delivering
# (measured 2026-10-19: dead egress tunnel, zero bytes for 300s), and three
# consecutive failures cordon the bucket. A clean upstream cut ("cut") is
# an upstream-content failure a healthy tunnel merely forwarded — its
# premature-death form surfaces as "interrupted" anyway (a tunnel that dies
# mid-chunk raises, it does not send a clean chunked terminator).
# Unknown outcomes default to failure so a new outcome can never silently
# count as success.
_STREAM_OUTCOMES: dict[str, tuple[bool, str]] = {
    "completed": (True, ""),
    "disconnected": (True, "client_disconnected"),
    "cut": (True, "stream_cut"),
    "stalled": (False, "stream_stalled"),
    "interrupted": (False, "stream_interrupted"),
}


class ZenDirectAdapter:
    name = "zen-direct"

    def __init__(self, settings: Settings, pool: EgressPool | None = None) -> None:
        self._settings = settings
        # 0.7.x: when the egress pool is configured, chat requests leave
        # through the per-bucket device agents instead of this local client
        # (the container itself has no free-tier quota — every bucket is a
        # device's public IP). list_models still uses the local client.
        self._pool = pool
        base = settings.zen_base_url
        if (urlparse(base).hostname or "") not in ("opencode.ai", "localhost", "127.0.0.1"):
            raise ValueError(
                "OPENCODE_ZEN_BASE_URL host not allowed for zen-direct "
                f"(got {base!r}; use UPSTREAM_MODE=openai for arbitrary hosts)"
            )
        proxy = settings.zen_proxy or None
        # Absolute upstream base, used to build request URLs. The pool's
        # per-egress clients carry no base_url (they exist only to route through
        # a device proxy), so upstream paths must be absolute or httpx raises
        # "Request URL is missing an 'http://' or 'https://' protocol".
        self._base = base.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(float(settings.zen_timeout_s), connect=15.0),
            proxy=proxy,
            # trust_env defaults to False: following ambient HTTP_PROXY /
            # HTTPS_PROXY couples the egress path to machine state. Measured
            # 2026-10-01: a stale HTTP_PROXY (dead Clash) made every upstream
            # call fail with ConnectError in ~2s and the bucket 502'd while
            # its network was fine. Route explicitly with ZEN_PROXY instead;
            # set ZEN_TRUST_ENV=1 only if you really want env/registry proxies.
            trust_env=settings.upstream_trust_env,
        )
        self._auth_free = not settings.zen_api_key
        # 40-hex workspace hash shape for `x-opencode-project` (see
        # _headers). A previously captured genuine value can be pinned via
        # OPENCODE_ZEN_PROJECT_ID; the default is a fixed instance-level
        # sha1 so the header stays byte-stable across requests.
        self._project_id = settings.zen_project_id or _project_hash()
        # tool_choice "auto" + stream_options mirror the genuine client body.
        self._builtin_tools = _builtin_tools()
        self._builtin_names = _tool_names(self._builtin_tools)
        # Upstream transport stack (see zen_bun_transport): the bun/boringssl
        # forwarder mirrors the genuine client runtime since the 2026-10-24
        # transport-fingerprint gate; httpx stays for dev/tests fallback.
        self._bun = BunTransport.from_settings(settings)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ API

    async def list_models(self) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        try:
            resp = await self.client.get(
                "/models",
                headers=self._headers(request_id=False),
            )
            if resp.status_code == 200:
                body = resp.json()
                for m in body.get("data", []):
                    if isinstance(m, dict) and m.get("id"):
                        data.append(
                            {
                                "id": f"opencode/{m['id']}",
                                "object": "model",
                                "created": 0,
                                "owned_by": "opencode-zen",
                            }
                        )
        except httpx.HTTPError:
            logger.warning("zen /models unreachable; falling back to static list")
        if not data:
            data = [
                {
                    "id": m,
                    "object": "model",
                    "created": 0,
                    "owned_by": "opencode-zen",
                }
                for m in self._settings.zen_models
            ]
        return {
            "object": "list",
            "data": data,
            "metadata": {"adapter": self.name, "source": "opencode-zen"},
        }

    async def chat(self, payload: dict[str, Any]) -> Any:
        """Returns an OpenAI JSON body, a StreamRelay, or an error-relay dict.

        The free tier only serves `stream: true` requests (verified by
        ablation: identical bodies with stream=false get 403). The adapter
        therefore ALWAYS streams upstream: streaming clients get the SSE
        relayed verbatim; non-streaming clients get the SSE aggregated into
        a single chat-completion JSON.

        With an egress pool configured every request leases one bucket first
        and releases it with the observed outcome, so bad buckets are
        cordoned and least-in-flight selection spreads load across healthy
        egress IPs.

        Bucket failover: an upstream 429 / 5xx / transport failure, or a 403
        FreeTierError in anonymous mode (kept defensively: with the corrected
        4-real-names shape this should not fire; if it does — e.g. the gate
        tightens again — failing over plus cordon recovery matches the
        2026-10-17 behaviour), releases the lease and retries on the next
        lease — none of these outcomes depend on the request itself, and no
        client-visible byte has been sent (the SSE stream had not started),
        so another bucket can serve the same body. Retryable errors never
        retry after bytes reached the client, and the loop is bounded so a
        fully-drained pool answers instead of hanging; a final free-tier 403
        is answered as 503 `egress_unavailable` (retryable) rather than
        relayed as a client-blaming 403.

        Per-attempt session mint: the upstream treats ``x-opencode-session``
        as single-use — replaying a consumed session returns the
        quota-shaped ``429 FreeUsageLimitError`` even on a healthy egress IP
        with byte-identical headers/body (live 2026-10-24: the same IP
        served 200 to a fresh mint and 429 to its reuse minutes later, and
        back-to-back 200s kept arriving on that IP to fresh mints). Headers
        are therefore minted inside the attempt loop — one mint per upstream
        request, matching the genuine client's behaviour.
        """
        model = self._zen_model(payload.get("model"))
        body = self._build_body(payload, model)
        body["stream"] = True
        stream = bool(payload.get("stream"))

        if self._pool is None:
            result = await self._exchange(
                self.client, body, self._headers(), model, stream, None
            )
            return self._egress_gate_answer(result)

        attempts = min(self._pool.size, _FAILOVER_MAX_ATTEMPTS)
        lease = await self._pool.acquire()
        while True:
            try:
                result = await self._exchange(
                    lease.client, body, self._headers(), model, stream, lease
                )
            except (httpx.HTTPError, ConnectionError) as exc:
                # _exchange already released the lease as a transport
                # failure (it wraps httpx transport errors in a plain
                # ConnectionError).
                attempts -= 1
                if attempts <= 0:
                    raise ConnectionError(f"zen request failed: {exc}") from exc
                _log_failover(kind="transport", remaining=attempts)
                lease = await self._pool.acquire()
                continue
            if isinstance(result, StreamRelay):
                return result
            status = result.get("__status__")
            if status == 429:
                attempts -= 1
                if attempts <= 0:
                    return result
                _log_failover(kind="http_429", remaining=attempts)
                lease = await self._pool.acquire()
                continue
            if isinstance(status, int) and status >= 500:
                attempts -= 1
                if attempts <= 0:
                    return result
                _log_failover(kind=f"http_{status}", remaining=attempts)
                lease = await self._pool.acquire()
                continue
            if (
                self._auth_free
                and _is_free_tier_rejection(status, result.get("content"))
            ):
                # Defensive failover on a shape-gate rejection. With the
                # >= 4 real-name toolset injected this should not fire; if
                # the gate tightens again, the cordons + rotator recover
                # automatically. (2026-10-24: the shape gate is decided
                # BEFORE the quota layer — such a 403 can also appear with
                # a quota-dead IP, so it is not necessarily egress-level.)
                attempts -= 1
                if attempts <= 0:
                    # Egress-level rejection with no attempt left to spend:
                    # answer the retryable 503, never relay the 403.
                    return self._egress_gate_answer(result)
                _log_failover(kind="http_403_free_tier", remaining=attempts)
                lease = await self._pool.acquire()
                continue
            # 200 body, StreamRelay-unwrapped result, or a request-level
            # 4xx: the outcome is final for this request.
            return result

    def _egress_gate_answer(self, result: Any) -> Any:
        """Map a final anonymous free-tier 403 onto a client-facing 503.

        Kept from 0.7.5/2026-10-17 as a defensive UX rewrite: a relayed 403
        makes clients blame their own credentials and stop retrying. With
        the 2026-10-24 shape fix the branch should remain cold; if the
        upstream ever re-tightens, the 503 keeps hermes-style clients in the
        retry loop while the pool recovers. Keyed mode and other 403 bodies
        pass through untouched: they really are request-level answers.
        """
        if (
            isinstance(result, dict)
            and self._auth_free
            and _is_free_tier_rejection(
                result.get("__status__"), result.get("content")
            )
        ):
            logger.warning(
                "free tier egress gate final; answering 503 to the client"
            )
            return _egress_unavailable_result()
        return result

    async def _exchange(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Route one upstream attempt through the configured transport.

        The bun forwarder (boringssl TLS + HTTP/2) is the wire-accurate
        mirror of the genuine client since the 2026-10-24 upstream
        transport-fingerprint gate; httpx remains the dev/tests fallback
        (deployed environments ship the bun binary; gate off via
        ``ZEN_BUN_TRANSPORT=0``).
        """
        if self._bun is not None:
            return await self._exchange_via_bun(
                self._bun, body, headers, model, stream, lease
            )
        return await self._exchange_via_httpx(
            client, body, headers, model, stream, lease
        )

    async def _exchange_via_bun(
        self,
        bun: BunTransport,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Send one upstream attempt through the bun transport subprocess.

        Wire parity with the genuine client: bun's transport (boringssl,
        HTTP/2) is REQUIRED on the anonymous free tier since 2026-10-24 —
        the same fresh egress IP served 200s to bun replays of byte-exact
        captures while every httpx attempt got the quota-shaped 429 (live
        cross-check; ablation summary in zen_bun_transport.py). Status
        classification, release kinds and the failover ladder are
        identical to the httpx path.
        """
        try:
            resp = await bun.post(
                base_url=self._base,
                path="/chat/completions",
                headers=headers,
                body=json.dumps(body).encode("utf-8"),
                proxy_url=self._egress_proxy_url(lease),
                timeout_s=float(self._settings.zen_timeout_s),
            )
        except BunTransportError as exc:
            await self._release(lease, ok=False, kind="transport")
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            raw = b"".join([chunk async for chunk in resp.aiter_bytes()])
            result = _raw_relay_body(resp.status_code, raw)
            if resp.status_code == 429:
                await self._release(
                    lease,
                    ok=False,
                    kind="http_429",
                    retry_after_s=_retry_after_seconds(resp.headers),
                )
            elif resp.status_code >= 500:
                await self._release(lease, ok=False, kind=f"http_{resp.status_code}")
            elif (
                resp.status_code == 403
                and self._auth_free
                and _is_free_tier_rejection(resp.status_code, result.get("content"))
            ):
                # Defensive release: identical to the httpx path. A 403 on
                # the real stack is no longer expected (shape gate sat); it
                # only means a parity-breaking upstream future-tightening.
                await self._release(lease, ok=False, kind="free_tier_rejected")
            else:
                await self._release(lease, ok=True)
            return result
        if stream:
            return self._stream_relay(resp, float(self._settings.stream_stall_s), lease)
        try:
            result = await _aggregate_sse(resp, model)
        except Exception:
            await self._release(lease, ok=False, kind="aggregate_error")
            raise
        else:
            await self._release(lease, ok=True)
            return result
        finally:
            await resp.aclose()

    async def _exchange_via_httpx(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Send one upstream attempt and fold the outcome into the lease.

        Fallback transport (dev/tests, no-bun images). The anonymous free
        tier's transport-fingerprint gate (2026-10-24) makes this stack
        unreliable in production: see `_exchange_via_bun`.
        """
        req = client.build_request(
            "POST", f"{self._base}/chat/completions", json=body, headers=headers
        )
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await self._release(lease, ok=False, kind="transport")
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            raw = await resp.aread()
            await resp.aclose()
            result = _raw_relay_body(resp.status_code, raw)
            if resp.status_code == 429:
                await self._release(
                    lease,
                    ok=False,
                    kind="http_429",
                    retry_after_s=_retry_after_seconds(resp.headers),
                )
            elif resp.status_code >= 500:
                await self._release(lease, ok=False, kind=f"http_{resp.status_code}")
            elif (
                resp.status_code == 403
                and self._auth_free
                and _is_free_tier_rejection(
                    resp.status_code, result.get("content")
                )
            ):
                # Defensive release: kept from 0.7.5 (the 2026-10-17 era).
                # With the 2026-10-24 shape fix in place this branch is
                # expected to stay cold; keeping the cordon makes any
                # future gate tightening self-healing via the rotator.
                await self._release(lease, ok=False, kind="free_tier_rejected")
            else:
                await self._release(lease, ok=True)
            return result
        if stream:
            return self._stream_relay(resp, float(self._settings.stream_stall_s), lease)
        try:
            result = await _aggregate_sse(resp, model)
        except Exception:
            await self._release(lease, ok=False, kind="aggregate_error")
            raise
        else:
            await self._release(lease, ok=True)
            return result
        finally:
            await resp.aclose()

    async def _release(
        self,
        lease: Lease | None,
        *,
        ok: bool,
        kind: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        if lease is not None and self._pool is not None:
            await self._pool.release(
                lease, ok=ok, kind=kind, retry_after_s=retry_after_s
            )

    def _stream_relay(
        self,
        resp: ResponseLike,
        stall_s: float,
        lease: Lease | None,
    ) -> StreamRelay:
        """Build the streaming relay + pool release hook (shared transports).

        The close hook folds the stream outcome into the egress pool so a
        bucket that stopped delivering is culled; in-flight accounting
        spans the whole stream either way.
        """
        if lease is None:
            return StreamRelay(resp, {}, stall_s=stall_s)
        pool = self._pool
        assert pool is not None
        held: Lease = lease

        async def _released(outcome: str) -> None:
            ok, kind = _STREAM_OUTCOMES.get(outcome, (False, outcome))
            if not ok:
                # Attribution: which egress stopped delivering? StreamRelay
                # already logged the mechanism (stall/interrupt); this line
                # ties it to the bucket that must be culled.
                logger.warning(
                    "upstream stream failed mid-way",
                    extra={"egress": held.name, "reason": kind},
                )
            await pool.release(held, ok=ok, kind=kind)

        return StreamRelay(resp, {}, on_close=_released, stall_s=stall_s)

    def _egress_proxy_url(self, lease: Lease | None) -> str | None:
        """HTTP-proxy URL for the leased bucket's agent (bun subprocess env).

        Mirrors ``EgressPool._proxy_for``: the tailnet is private but the
        agent still requires Basic auth on CONNECT; credentials come from
        ``zen_egress_secret`` and are composed per attempt, so no secret
        value is stored in specs, admin snapshots or logs.
        """
        if lease is None:
            return None
        agent = lease.state.spec.agent_url
        secret = self._settings.zen_egress_secret
        if not secret:
            return agent
        parsed = urlparse(agent)
        creds = quote_plus(secret)
        return f"{parsed.scheme}://{creds}:{creds}@{parsed.netloc}{parsed.path or ''}"

    # ------------------------------------------------------------ internals

    @staticmethod
    def _zen_model(model: Any) -> str:
        raw = str(model or "")
        _provider, _sep, model_id = raw.partition("/")
        return model_id if _sep else (raw or "big-pickle")

    def _headers(self, *, request_id: bool = True) -> dict[str, str]:
        """Reconstructed protocol headers, in the 1.19.4 capture's order."""
        version = self._settings.zen_client_version
        ua = (
            f"opencode/{version} ai-sdk/provider-utils/4.0.23 "
            f"runtime/bun/{self._settings.zen_bun_version}"
        )
        # Session ids are minted DESCENDING (~((ms<<12)+counter)) since
        # opencode 1.19.0; the shape gate rejects ascending session ids
        # with FreeTierError (verified live 2026-10-13, re-verified via
        # byte-clone 2026-10-24). Request ids stay ascending, matching the
        # genuine client. The genuine 1.19.4 client sends the minted value
        # TWICE (`x-opencode-session` + `x-opencode-session-id`) and no
        # legacy `x-session-id` stable header at all (the 2026-09 Zen
        # header had already been dropped upstream by the 1.19.4 build).
        session = opencode_id("ses_", descending=True)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": ua,
            "x-opencode-client": "cli",
            # The captured client carries a 40-hex workspace hash here
            # (sha1 of the workspace directory). "global" was fine in the
            # 1.18 era but has no 2026-10-24 pass datapoint, so mirror the
            # captured shape with a fixed instance-level hash.
            "x-opencode-project": self._project_id,
            "x-opencode-session": session,
            "x-opencode-session-id": session,
            # Captured genuine client sends the well-known `Bearer public` on
            # the anonymous free tier (the console proxy strips anonymous auth
            # before the inference service sees it, so it is present on the
            # wire but not effective). A configured Zen key replaces it.
            "Authorization": "Bearer public",
        }
        if self._settings.zen_api_key:
            headers["Authorization"] = f"Bearer {self._settings.zen_api_key}"
        if request_id:
            headers["x-opencode-request"] = opencode_id("msg_")
        return headers

    def _build_body(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = model
        messages = _clean_messages(body.get("messages") or [])
        if not messages:
            raise ValueError(
                "messages must contain at least one usable turn "
                "(content or tool_calls)"
            )
        client_tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
        if self._auth_free:
            mode = self._settings.zen_marker_mode
            if mode == "full":
                messages = self._inject_marker(messages)
            elif mode == "bridge":
                messages = self._inject_bridge(messages)
            # "none": leave the client's messages untouched
            body["tools"] = self._merge_tools(client_tools)
            if "tool_choice" not in body and body["tools"]:
                body["tool_choice"] = "auto"
        else:
            body["tools"] = client_tools
        if not body.get("tools"):
            body.pop("tools", None)
            body.pop("tool_choice", None)
        body["messages"] = messages
        # mirror the genuine client's stream_options (chat() forces stream)
        if "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _inject_marker(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prepend the captured opencode prompt (legacy compatibility option)."""
        marker: dict[str, Any] = {
            "role": "system",
            "content": _default_prompt() + _BRIDGE_NOTE,
        }
        return [marker, *messages]

    @staticmethod
    def _inject_bridge(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prepend only the tiny tool-steering note (no opencode prompt)."""
        marker: dict[str, Any] = {
            "role": "system",
            "content": _BRIDGE_NOTE_STANDALONE,
        }
        return [marker, *messages]

    def _merge_tools(self, client_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Builtin schemas first (required by the gate), client tools after.

        `ZEN_TOOLS_MODE=minimal` (default) injects two synthesized ~30-token
        schemas under builtin names the client is not already using;
        `captured2` injects the {bash, read} subset of the captured asset;
        `all` injects the full captured 11-tool list (legacy behaviour, which
        also drops a builtin whose name a client tool claims).
        """
        mode = self._settings.zen_tools_mode
        client_names = _tool_names(client_tools)

        if mode == "all":
            injected = [
                t for t in self._builtin_tools
                if not (_tool_names([t]) & client_names)
            ]
        elif mode == "captured2":
            injected = [
                t for t in self._builtin_tools
                if _tool_names([t]) & set(_MINIMAL_TOOL_NAMES)
                and not (_tool_names([t]) & client_names)
            ]
        else:  # minimal
            injected = []
            for name in _INJECT_NAME_POOL:
                if name in client_names:
                    continue
                injected.append(_minimal_schema(name))
                if len(injected) >= _MIN_INJECTED_TOOLS:
                    break
            if len(injected) < _MIN_INJECTED_TOOLS:
                # The client claims every pool name (pathological). Fall back
                # to the captured subset so the gate still sees >= 2 names.
                injected = [
                    t for t in self._builtin_tools
                    if _tool_names([t]) & set(_MINIMAL_TOOL_NAMES)
                ]
        return [*injected, *client_tools]


def _merge_tool_call_delta(
    acc: dict[int, dict[str, Any]], delta: dict[str, Any]
) -> None:
    """Merge one streamed tool_call delta into the per-index accumulator."""
    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        idx = int(tc.get("index") or 0)
        slot = acc.setdefault(
            idx,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


async def _aggregate_sse(resp: ResponseLike, model: str) -> dict[str, Any]:
    """Aggregate one upstream SSE stream into a non-stream completion JSON."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    completion_id = ""
    created = int(time.time())

    async for line in resp.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue  # tolerate the non-standard {"choices":[],"cost":"0"} tail
        if not isinstance(chunk, dict):
            continue
        if chunk.get("id"):
            completion_id = str(chunk["id"])
        if chunk.get("created"):
            created = int(chunk["created"])
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(str(delta["content"]))
            if delta.get("reasoning_content"):
                reasoning_parts.append(str(delta["reasoning_content"]))
            _merge_tool_call_delta(tool_calls, delta)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    ordered = [tool_calls[i] for i in sorted(tool_calls)]
    if ordered:
        message["tool_calls"] = ordered
    if tool_calls and finish_reason is None:
        finish_reason = "tool_calls"
    # Silent upstream cut: content arrived but the stream ended without a
    # terminal chunk (see StreamRelay). Report it as an output-cap
    # truncation — never stamp "stop" on a cut generation.
    cut = finish_reason is None and bool(content_parts or reasoning_parts)
    final_reason = finish_reason or ("length" if cut else "stop")
    return {
        "id": completion_id or f"chatcmpl-zen-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": final_reason}
        ],
        "usage": usage,
        "metadata": {
            "adapter": "zen-direct",
            "aggregated_stream": True,
            **({"upstream_stream_cut": True} if cut else {}),
        },
    }
