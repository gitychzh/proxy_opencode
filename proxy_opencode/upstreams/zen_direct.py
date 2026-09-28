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

  * Free tier gate (reverse-engineered 2026-09-27 via ablation on live
    requests; every combination below verified reproducibly):
        system prompt marker + builtin tool schemas   -> 200
        marker only (no builtin tools)                -> 403 (CN egress)
                                                        429/pass (non-CN)
        builtin tools only (no marker system)         -> 403
        custom tools only                             -> 403
        builtin + custom tools merged                 -> 200
    i.e. the server fingerprints the request BODY: it must carry BOTH
    opencode's genuine default system prompt (v1.18.32 `prompt/default.txt`,
    shipped as `zen_prompt_default.txt`) AND opencode's builtin tool schema
    list (`zen_builtin_tools.json`, captured from the genuine client). The
    adapter injects both when no API key is configured and merges the
    client's own tools on top. Egress IP region additionally matters:
    non-tunnelled CN egress needs the full marker set (see Settings.zen_proxy
    / trust_env note in the client constructor).

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
    low 48 bits, then 14 random chars drawn from [0-9A-Za-z] (byte % 62).
    """
    del descending  # opencode only uses ascending ids for sessions/messages
    now_ms = time.time_ns() // 1_000_000
    current = now_ms * 0x1000 + 1
    time_part = format(current & 0xFFFFFFFFFFFF, "012x")
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
    """Keep only OpenAI-core per-message fields (upstream rejects extras)."""
    keep = ("role", "content", "tool_calls", "tool_call_id", "name")
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        cleaned = {k: v for k, v in m.items() if k in keep and v is not None}
        out.append(cleaned)
    return out


class ZenDirectAdapter:
    name = "zen-direct"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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
            # trust_env stays ON deliberately: on Windows httpx then follows
            # the registry/env system proxy (e.g. Clash), which changes the
            # egress region the free-tier gate inspects. An explicit
            # ZEN_PROXY always wins. Set to False only for direct egress.
            trust_env=True,
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
        """
        model = self._zen_model(payload.get("model"))
        body = self._build_body(payload, model)
        body["stream"] = True
        headers = self._headers()

        stream = bool(payload.get("stream"))
        req = self.client.build_request(
            "POST", "/chat/completions", json=body, headers=headers
        )
        try:
            resp = await self.client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            raw = await resp.aread()
            await resp.aclose()
            return _raw_relay_body(resp.status_code, raw)
        if stream:
            return StreamRelay(resp, {})
        try:
            return await _aggregate_sse(resp, model)
        finally:
            await resp.aclose()

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
            "x-opencode-session": opencode_id("ses_"),
        }
        if request_id:
            headers["x-opencode-request"] = opencode_id("msg_")
        return headers

    def _build_body(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = model
        messages = _clean_messages(body.get("messages") or [])
        client_tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
        if self._auth_free:
            messages = self._inject_marker(messages)
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
        """Prepend opencode's genuine system prompt (free-tier requirement)."""
        marker: dict[str, Any] = {
            "role": "system",
            "content": _default_prompt() + _BRIDGE_NOTE,
        }
        return [marker, *messages]

    def _merge_tools(self, client_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Builtin schemas first (required by the gate), client tools after.

        Name collisions (client tool named like a builtin) resolve in favour
        of the client tool; the builtin with the same name is dropped.
        """
        client_names = _tool_names(client_tools)
        merged = [t for t in self._builtin_tools if not (_tool_names([t]) & client_names)]
        return [*merged, *client_tools]


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
    return {
        "id": completion_id or f"chatcmpl-zen-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
        ],
        "usage": usage,
        "metadata": {"adapter": "zen-direct", "aggregated_stream": True},
    }


def timestamps() -> tuple[int, str]:
    return int(time.time()), f"chatcmpl-zen-{secrets.token_hex(8)}"
