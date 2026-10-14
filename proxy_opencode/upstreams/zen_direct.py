"""zen-direct adapter: talks to OpenCode Zen's OpenAI-compatible API directly.

UPSTREAM_MODE=zen-direct reconstructs the exact client protocol that the
official opencode 1.18.x client sends to `https://opencode.ai/zen/v1`:

  * Endpoint: POST {base}/chat/completions (SSE streaming supported).
  * Auth: `Authorization: Bearer public` for the anonymous free tier, or a
    real Zen API key when OPENCODE_ZEN_API_KEY is set.
  * Protocol headers (verified byte-for-byte against a mitm capture of the
    genuine client):
        Authorization: Bearer public
        x-session-id: <stable UUID>   (Zen gate since 2026-09; without it
                                        anonymous requests get AuthError
                                        "Missing API key")
        Content-Type: application/json
        User-Agent: opencode/<ver> ai-sdk/provider-utils/4.0.23 runtime/bun/<ver>
        x-opencode-client: cli
        x-opencode-project: global
        x-opencode-session: ses_<26 chars>
        x-opencode-request: msg_<26 chars>
  * Identifiers replicate opencode's `packages/schema/src/identifier.ts`:
    26 chars = 12 hex chars of `(ms << 12) + counter` (low 48 bits) followed
    by 14 random chars from [0-9A-Za-z].

  * Anonymous free-tier checks (live-verified 2026-10-13 against
    opencode 1.19.0 / the inference-service cutover):
      - `stream: true` and >= 2 tools whose names are opencode builtin
        names are still required (unchanged from the 2026-09-30 ablation).
      - NEW: the `x-opencode-session` header must be a *genuinely
        opencode-shaped* session id: the 12-hex time part must be the
        bitwise-INVERTED (descending) encoding of `(ms << 12) + counter`
        (schema/src/session-id.ts uses `descending()`), and the counter
        must stay below 64. Ascending session ids — what this adapter
        emitted until 0.6.4 — are rejected with HTTP 403 FreeTierError
        "OpenCode's free tier can only be used from within OpenCode".
        `x-opencode-request` (msg_) stays ascending; no counter limit.
      - The client's other headers are NOT checked: UA version, cli vs
        desktop, x-opencode-project value, x-session-id, Accept, and the
        marker prompt all passed either way.

Everything else is a faithful OpenAI passthrough: messages/params are
forwarded via the shared whitelist, SSE chunks are relayed as-is, and
upstream errors are relayed with their original status code.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings
from ..egress.pool import EgressPool, Lease
from .openai_http import StreamRelay, _raw_relay_body

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

# Minimal builtin schema set that still satisfies the gate.
#
# Live ablation on 2026-09-30 (ubuntu26 egress, reproducible) established the
# exact rule: the gate requires **>= 2 tools whose NAMES are opencode builtin
# names**, and ignores their schemas completely.
#
#   bash+read, full captured schemas (7,872 ch)  -> 200
#   bash+read, name + empty params   (246 ch)    -> 200
#   bash+read, name only             (104 ch)    -> 200
#   bash only                        (123 ch)    -> 403
#   two non-builtin names            (~250 ch)   -> 403
#   bash+read+edit                   (369 ch)    -> 200
#
# So the mandatory fingerprint costs ~60 prompt tokens instead of ~1,970:
# `ZEN_TOOLS_MODE=minimal` injects two synthesized tiny schemas.
# `captured2` restores the old 2-tool subset of the captured asset and `all`
# the full 11-tool list, as fallbacks if the gate ever tightens again.
#
# A SECOND, independent gate appeared with the 2026-10-11 inference-service
# cutover (first hit 2026-10-13): the `x-opencode-session` id must be a
# descending-encoded opencode session id with counter < 64, else FreeTierError
# 403. See opencode_id() and the module docstring.
_MINIMAL_TOOL_NAMES = ("bash", "read")

_MIN_INJECTED_TOOLS = 2

# Injected names, in preference order. A name the client already uses is
# skipped: injecting a duplicate would not add a distinct builtin name (the
# gate counts names, not entries) and the old code dropped the builtin in that
# case, which could push the count below two.
_INJECT_NAME_POOL = (
    "bash", "read", "todowrite", "skill", "task", "list",
    "webfetch", "websearch", "glob", "grep", "edit", "write",
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
        # tool_choice "auto" + stream_options mirror the genuine client body.
        self._builtin_tools = _builtin_tools()
        self._builtin_names = _tool_names(self._builtin_tools)

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
        """
        model = self._zen_model(payload.get("model"))
        body = self._build_body(payload, model)
        body["stream"] = True
        headers = self._headers()
        stream = bool(payload.get("stream"))

        if self._pool is None:
            return await self._exchange(self.client, body, headers, model, stream, None)
        lease = await self._pool.acquire()
        return await self._exchange(lease.client, body, headers, model, stream, lease)

    async def _exchange(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Send one upstream request and fold the outcome into the lease.

        A 4xx other than 429 (400/403/… ) is a request-level rejection the
        upstream returned through a working egress, so it releases the lease
        as a success; only 429 (free-tier quota) and 5xx / transport failures
        count against the bucket. Mirrors the balancer's release semantics.
        """
        req = client.build_request(
            "POST", "/chat/completions", json=body, headers=headers
        )
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await self._release(lease, ok=False, kind="transport")
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            raw = await resp.aread()
            await resp.aclose()
            if resp.status_code == 429:
                await self._release(lease, ok=False, kind="http_429")
            elif resp.status_code >= 500:
                await self._release(lease, ok=False, kind=f"http_{resp.status_code}")
            else:
                await self._release(lease, ok=True)
            return _raw_relay_body(resp.status_code, raw)
        if stream:
            if lease is None:
                return StreamRelay(resp, {})
            pool = self._pool
            assert pool is not None
            held: Lease = lease

            async def _released() -> None:
                await pool.release(held, ok=True)

            return StreamRelay(resp, {}, on_close=_released)
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

    async def _release(self, lease: Lease | None, *, ok: bool, kind: str = "") -> None:
        if lease is not None and self._pool is not None:
            await self._pool.release(lease, ok=ok, kind=kind)

    # ------------------------------------------------------------ internals

    @staticmethod
    def _zen_model(model: Any) -> str:
        raw = str(model or "")
        _provider, _sep, model_id = raw.partition("/")
        return model_id if _sep else (raw or "big-pickle")

    def _headers(self, *, request_id: bool = True) -> dict[str, str]:
        """Reconstructed protocol headers, in the captured order."""
        version = self._settings.zen_client_version
        ua = (
            f"opencode/{version} ai-sdk/provider-utils/4.0.23 "
            f"runtime/bun/{self._settings.zen_bun_version}"
        )
        headers = {
            "Authorization": f"Bearer {self._settings.zen_api_key or 'public'}",
            # Session gate (Zen, 2026-09): anonymous free-tier requests are
            # rejected with AuthError "Missing API key" unless a stable
            # `x-session-id` UUID header is present.
            "x-session-id": self._settings.zen_session_id,
            "Content-Type": "application/json",
            "User-Agent": ua,
            "x-opencode-client": "cli",
            "x-opencode-project": "global",
            # Session ids are minted DESCENDING (~((ms<<12)+counter)) since
            # opencode 1.19.0; the free-tier gate rejects ascending session
            # ids with FreeTierError (verified live 2026-10-13). Request ids
            # stay ascending, matching the genuine client.
            "x-opencode-session": opencode_id("ses_", descending=True),
        }
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


async def _aggregate_sse(resp: httpx.Response, model: str) -> dict[str, Any]:
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
