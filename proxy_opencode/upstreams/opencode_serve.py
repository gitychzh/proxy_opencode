"""opencode-serve adapter: official local `opencode serve` HTTP API.

Drives the CURRENT opencode server API (v2 `/api` surface), verified live
against opencode 1.18.x:

    POST /api/session                      -> create session (model, permission)
    POST /api/session/{id}/prompt          -> {"prompt": {"text": ...}}
    GET  /api/session/{id}/message (poll)  -> finished when assistant.finish set
    GET  /api/session/{id}/message         -> {"data": [Message, ...]}
    DELETE /session/{id}                   -> cleanup (ephemeral sessions)

Bridging semantics (the reason this gateway exists):
  * External function-calling tools are NOT native to the serve prompt API.
    They are bridged via a JSON contract injected into the system prompt and
    parsed out of the assistant's reply (see tools_contract.py).
  * big-pickle is a reasoning model; its reasoning parts are mapped to
    OpenAI `reasoning_content`.
  * The serve API has no token streaming; `stream=true` gets a synthetic
    chunked response once the turn completes (metadata.synthetic_stream).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from . import tools_contract

logger = logging.getLogger("proxy_opencode.serve")


class ServeError(Exception):
    """opencode serve returned an unexpected/non-2xx response."""


class WaitTimeoutError(Exception):
    """`/wait` stayed busy past OPENCODE_SERVE_WAIT_TIMEOUT_S."""


# Built-in agent tools must stay in the request schema (removing them makes
# Zen's free-tier check reject the call), but their EXECUTION is denied via a
# session permission ruleset, so the agent cannot hang on an interactive
# approval prompt. Client-facing tools go through the tools_contract bridge.
_BUILTIN_TOOLS_DENY = [
    "question", "bash", "read", "glob", "grep", "edit", "write",
    "task", "webfetch", "todowrite", "websearch", "skill",
    "apply_patch", "invalid", "list", "patch", "ls",
]


@dataclass
class ServeCompletion:
    """Normalised result of one completed assistant turn."""

    text: str
    reasoning: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model_id: str = ""
    internal_tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ServeAdapter:
    """Drives the local official opencode serve. Stateless per request
    (each request maps to one ephemeral opencode session)."""

    name = "opencode-serve"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.opencode_serve_url,
            timeout=httpx.Timeout(
                float(settings.opencode_serve_timeout_s), connect=10.0
            ),
            auth=httpx.BasicAuth(
                settings.opencode_server_username,
                settings.opencode_server_password,
            ),
            headers={"Content-Type": "application/json"},
        )
        # Serialize whole chat turns: one opencode server processes one
        # session; parallel turns on a free tier only trip rate limits.
        self._turn_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ API

    async def list_models(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": m,
                    "object": "model",
                    "created": 0,
                    "owned_by": "opencode-serve",
                }
                for m in self._settings.opencode_serve_models
            ],
            "metadata": {
                "adapter": self.name,
                "source": "OPENCODE_SERVE_MODELS",
            },
        }

    async def chat(self, payload: dict[str, Any]) -> ServeCompletion:
        async with self._turn_lock:
            return await asyncio.wait_for(
                self._run_turn(payload),
                timeout=self._settings.opencode_serve_wait_timeout_s + 30,
            )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _parse_model(model: str | None) -> tuple[str, str]:
        if not model:
            return "opencode", "big-pickle"
        provider, sep, model_id = model.partition("/")
        if not sep or not provider or not model_id:
            raise ServeError(
                f"model {model!r} must be 'provider/id' (e.g. opencode/big-pickle)"
            )
        return provider, model_id

    async def _api(self, method: str, url: str, **kw: Any) -> Any:
        try:
            resp = await self.client.request(method, url, **kw)
        except httpx.TimeoutException as exc:
            raise ConnectionError("opencode serve request timed out") from exc
        except httpx.HTTPError as exc:
            raise ConnectionError(f"cannot reach opencode serve: {exc}") from exc
        if resp.status_code == 401:
            raise ServeError(
                "opencode serve returned 401; check OPENCODE_SERVER_PASSWORD"
            )
        if resp.status_code >= 400:
            raise ServeError(
                f"opencode serve returned {resp.status_code} for {method} {url}: "
                f"{resp.text[:300]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception as exc:
            raise ServeError(
                f"opencode serve returned non-JSON for {method} {url}"
            ) from exc

    async def _wait_idle(self, sid: str) -> list[dict[str, Any]]:
        # /wait long-polls for events and 503s spuriously; polling the message
        # list for a finished assistant message is the reliable signal.
        deadline = asyncio.get_running_loop().time() + float(
            self._settings.opencode_serve_wait_timeout_s
        )
        while True:
            msgs = await self._api("GET", f"/api/session/{sid}/message")
            data = list((msgs or {}).get("data", []))
            for m in data:
                if (
                    isinstance(m, dict)
                    and m.get("type") == "assistant"
                    and (m.get("finish") or m.get("error"))
                ):
                    return data
            if asyncio.get_running_loop().time() > deadline:
                raise WaitTimeoutError(
                    "opencode serve turn did not finish within "
                    f"{self._settings.opencode_serve_wait_timeout_s}s"
                )
            await asyncio.sleep(1.0)

    async def _run_turn(self, payload: dict[str, Any]) -> ServeCompletion:
        provider, model_id = self._parse_model(payload.get("model"))
        messages = payload.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise ServeError("messages must be a non-empty array")

        tools = payload.get("tools") or []
        tool_choice = payload.get("tool_choice")
        reasoning_effort = payload.get("reasoning_effort")

        prompt_text = tools_contract.build_prompt(
            messages,
            tools=tools if isinstance(tools, list) else [],
            tool_choice=tool_choice,
            reasoning_effort=reasoning_effort,
        )

        session = await self._api(
            "POST",
            "/api/session",
            json={
                "model": {"providerID": provider, "id": model_id},
                "permission": [
                    {"permission": n, "action": "deny", "pattern": "*"}
                    for n in _BUILTIN_TOOLS_DENY
                ],
            },
        )
        sid = session["data"]["id"]
        try:
            attempts = 2 if tools else 1
            result = None
            for attempt in range(attempts):
                text = prompt_text
                if attempt > 0:
                    text = (
                        "CONTRACT VIOLATION. Your previous reply was prose, not "
                        "the required JSON. Reply NOW with exactly one JSON "
                        'object {"tool_calls": [...]} and nothing else. No '
                        "prose, no reasoning in the reply, no markdown."
                    )
                await self._api(
                    "POST",
                    f"/api/session/{sid}/prompt",
                    json={"prompt": {"text": text}, "delivery": "steer"},
                )
                data = await self._wait_idle(sid)
                result = self._extract(data, model_id)
                if tools:
                    parsed = tools_contract.parse_tool_calls(result.text)
                    if parsed is not None:
                        result.tool_calls = parsed
                        result.text = ""
                        break
            assert result is not None
        finally:
            if self._settings.opencode_serve_ephemeral_sessions:
                try:
                    await self._api("DELETE", f"/session/{sid}")
                except ServeError:
                    logger.warning("failed to delete ephemeral session %s", sid)
        return result

    @staticmethod
    def _extract(messages: list[dict[str, Any]], model_id: str) -> ServeCompletion:
        assistant = next(
            (
                m
                for m in messages
                if isinstance(m, dict)
                and m.get("type") == "assistant"
                and (m.get("finish") or m.get("error"))
            ),
            None,
        )
        if assistant is None:
            raise ServeError("no finished assistant message in session")
        if assistant.get("error"):
            raise ServeError(f"assistant turn failed: {assistant['error']}")

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        internal_tools: list[dict[str, Any]] = []
        for part in assistant.get("content", []):
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text_parts.append(part.get("text") or "")
            elif ptype == "reasoning":
                reasoning_parts.append(part.get("text") or "")
            elif ptype in ("tool", "tool_call"):
                internal_tools.append(part)

        tokens = assistant.get("tokens") or {}
        usage = {
            "prompt_tokens": int(tokens.get("input") or 0)
            + int((tokens.get("cache") or {}).get("read") or 0),
            "completion_tokens": int(tokens.get("output") or 0),
            "total_tokens": 0,
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        if tokens.get("reasoning"):
            usage["completion_tokens_details"] = {
                "reasoning_tokens": int(tokens["reasoning"])
            }

        return ServeCompletion(
            text="\n".join(p for p in text_parts if p),
            reasoning="\n".join(p for p in reasoning_parts if p),
            usage=usage,
            model_id=model_id,
            internal_tool_calls=internal_tools,
        )
