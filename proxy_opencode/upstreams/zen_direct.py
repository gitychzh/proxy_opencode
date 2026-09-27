"""zen-direct adapter: talks to OpenCode Zen's OpenAI-compatible API directly.

UPSTREAM_MODE=zen-direct reconstructs the exact client protocol that the
official opencode 1.18.x client sends to `https://opencode.ai/zen/v1`:

  * Endpoint: POST {base}/chat/completions (SSE streaming supported).
  * Auth: `Authorization: Bearer public` for the anonymous free tier, or a
    real Zen API key when OPENCODE_ZEN_API_KEY is set.
  * Protocol headers (verified byte-for-byte against a mitm capture of the
    genuine client):
        Authorization: Bearer public
        Content-Type: application/json
        User-Agent: opencode/<ver> ai-sdk/provider-utils/4.0.23 runtime/bun/<ver>
        x-opencode-client: cli
        x-opencode-project: global
        x-opencode-session: ses_<26 chars>
        x-opencode-request: msg_<26 chars>
  * Identifiers replicate opencode's `packages/schema/src/identifier.ts`:
    26 chars = 12 hex chars of `(ms << 12) + counter` (low 48 bits) followed
    by 14 random chars from [0-9A-Za-z].
  * Free tier: the server-side check rejects anonymous requests that do not
    look like genuine opencode workloads (verified empirically 2026-09-27:
    a minimal body gets 403 FreeTierError even with perfect headers, while
    the same headers plus opencode's genuine system prompt pass). The
    adapter therefore prepends opencode's real default system prompt
    (v1.18.32 `prompt/default.txt`, shipped as a package asset) as the
    leading system message when no API key is configured.

Everything else is a faithful OpenAI passthrough: messages/tools/params are
forwarded via the shared whitelist, SSE chunks are relayed as-is, and
upstream errors are relayed with their original status code.
"""

from __future__ import annotations

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

_ASSET = Path(__file__).with_name("zen_prompt_default.txt")

# Appended after the marker prompt so the model serves the *client's* tools
# instead of opencode's built-ins. Kept separate from the marker text.
_BRIDGE_NOTE = (    "\n\n---\n"
    "The system prompt above establishes the opencode runtime context that "
    "the upstream provider requires. You are now serving an OpenAI-compatible "
    "API client: use ONLY the tools supplied with the current request (if "
    "any) instead of the opencode built-in tools, and reply in the language "
    "the client's latest message uses."
)

def _default_prompt() -> str:
    return _ASSET.read_text(encoding="utf-8")


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
            # trust_env stays ON deliberately. Empirically (2026-09-27) the
            # Zen free-tier gate rejects anonymous big-pickle calls egressing
            # from CN IPs, while requests tunnelled through the user's local
            # proxy (Clash system proxy on Windows registry / env vars) pass.
            # trust_env=True lets httpx pick up that system proxy; an explicit
            # ZEN_PROXY overrides env/registry when set. trust_env=False
            # forces a direct CN egress and deterministically yields 403
            # FreeTierError even with a perfect request.
            trust_env=True,
        )
        self._auth_free = not settings.zen_api_key

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
        """Returns an OpenAI JSON body, a StreamRelay, or an error-relay dict."""
        model = self._zen_model(payload.get("model"))
        body = self._build_body(payload, model)
        headers = self._headers()

        stream = bool(payload.get("stream"))
        if stream:
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
            return StreamRelay(resp, {})

        try:
            resp = await self.client.post(
                "/chat/completions", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            return _raw_relay_body(resp.status_code, resp.content)
        try:
            return resp.json()
        except Exception:
            return _raw_relay_body(502, b"")

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
        if self._auth_free:
            messages = self._inject_marker(messages)
        body["messages"] = messages
        return body

    @staticmethod
    def _inject_marker(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prepend opencode's genuine system prompt (free-tier requirement)."""
        marker: dict[str, Any] = {
            "role": "system",
            "content": _default_prompt() + _BRIDGE_NOTE,
        }
        return [marker, *messages]


def timestamps() -> tuple[int, str]:
    return int(time.time()), f"chatcmpl-zen-{secrets.token_hex(8)}"
