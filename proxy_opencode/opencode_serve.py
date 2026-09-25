"""opencode-serve upstream adapter: local-only HTTP API of `opencode serve`.

This adapter talks to the official ``opencode serve`` HTTP server on the
local loopback interface only. It intentionally does NOT touch rate limits,
quotas, fingerprints, or free-tier mechanics of the server — all of that
stays under the control of the official server. Authentication is HTTP Basic
(``user:password``, default user ``opencode``, password from
``OPENCODE_SERVER_PASSWORD``).

Flow per chat completion: POST /api/session -> POST /api/session/{id}/prompt
(immediately admitted) -> POST /api/session/{id}/wait -> GET
/api/session/{id}/message and take the latest assistant message.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .opencode_cli import build_prompt


class ServeError(Exception):
    """The opencode-serve API returned an unexpected/non-2xx response."""


class WaitTimeoutError(Exception):
    """opencode serve /wait kept returning non-fatal statuses past the deadline."""


# /wait statuses that mean "still running": retry until the wait deadline.
_NON_FATAL_WAIT_STATUSES = frozenset({202, 409, 503})


@dataclass
class ServeResult:
    """Normalised result of one completed assistant turn."""

    text: str
    reasoning: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    session_id: str


def _auth(settings: Settings) -> httpx.BasicAuth:
    return httpx.BasicAuth(
        username=settings.opencode_server_username,
        password=settings.opencode_server_password,
    )


def basic_auth_header(settings: Settings) -> str:
    raw = f"{settings.opencode_server_username}:{settings.opencode_server_password}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def make_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.opencode_serve_url,
        timeout=httpx.Timeout(
            float(settings.opencode_serve_timeout_s), connect=10.0
        ),
        auth=_auth(settings),
        headers={"Content-Type": "application/json"},
    )


async def _request(client: httpx.AsyncClient, method: str, url: str, **kw) -> Any:
    resp = await client.request(method, url, **kw)
    if resp.status_code == 401:
        raise ServeError(
            "opencode serve returned 401; check OPENCODE_SERVER_PASSWORD"
        )
    if resp.status_code >= 400:
        raise ServeError(
            f"opencode serve returned {resp.status_code} for {method} {url}"
        )
    try:
        return resp.json()
    except Exception as exc:
        raise ServeError(
            f"opencode serve returned non-JSON for {method} {url}"
        ) from exc


def parse_model(model: str | None) -> tuple[str, str]:
    """Split 'provider/id'; default to opencode/big-pickle."""
    if not model:
        return "opencode", "big-pickle"
    provider, sep, model_id = model.partition("/")
    if not sep or not provider or not model_id:
        raise ServeError(
            f"model {model!r} must be 'provider/id' (e.g. opencode/big-pickle)"
        )
    return provider, model_id


def _render_tool_parts(
    parts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for i, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") != "tool":
            continue
        if not isinstance(part, dict):
            continue
        name = part.get("name") or part.get("tool") or "tool"
        args = part.get("input") or part.get("arguments") or {}
        call = {
            "id": str(part.get("id") or f"call_serve_{i}"),
            "type": "function",
            "function": {
                "name": str(name),
                "arguments": json.dumps(args, ensure_ascii=False)
                if not isinstance(args, str)
                else args,
            },
        }
        tool_calls.append(call)
        result: dict[str, Any] = {"name": str(name)}
        for key in ("output", "result", "error"):
            if key in part:
                result[key] = part[key]
        tool_results.append(result)
    return tool_calls, tool_results


def _extract_assistant(messages: Any) -> ServeResult:
    if isinstance(messages, dict):
        messages = messages.get("data", messages.get("messages", []))
    if not isinstance(messages, list):
        raise ServeError("unexpected /message payload shape")
    assistant = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            assistant = msg
            break
    if assistant is None:
        raise ServeError("no assistant message found after wait")
    content = assistant.get("content") or []
    if isinstance(content, str):
        return ServeResult(content, "", [], [], "")
    texts: list[str] = []
    reasonings: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            texts.append(str(part.get("text", "")))
        elif ptype == "reasoning":
            rtc = part.get("text") or part.get("reasoning") or ""
            reasonings.append(str(rtc))
    tool_calls, tool_results = _render_tool_parts(content)
    if not texts and not tool_calls:
        raise ServeError("assistant message has no text content")
    return ServeResult(
        "".join(texts), "".join(reasonings), tool_calls, tool_results, ""
    )


async def _poll_wait(
    client: httpx.AsyncClient, session_id: Any, settings: Settings
) -> None:
    """Poll POST /api/session/{id}/wait until the turn finishes (2xx).

    opencode serve returns 503/202/409 while the agent is still busy; those
    are non-fatal and retried with exponential backoff (0.5s -> 2s cap) until
    OPENCODE_SERVE_WAIT_TIMEOUT_S elapses. Other 4xx/5xx are fatal.
    """
    deadline = time.monotonic() + settings.opencode_serve_wait_timeout_s
    url = f"/api/session/{session_id}/wait"
    delay = 0.5
    while True:
        resp = await client.post(url, json={})
        if resp.status_code < 400:
            return
        if resp.status_code in _NON_FATAL_WAIT_STATUSES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeoutError(
                    f"opencode serve wait still {resp.status_code} after "
                    f"{settings.opencode_serve_wait_timeout_s}s"
                )
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 2.0)
            continue
        if resp.status_code == 401:
            raise ServeError(
                "opencode serve returned 401; check OPENCODE_SERVER_PASSWORD"
            )
        raise ServeError(
            f"opencode serve returned {resp.status_code} for POST {url}"
        )


async def _poll_wait(
    client: httpx.AsyncClient, session_id: str, settings: Settings
) -> None:
    """Poll POST /api/session/{id}/wait until 2xx.

    202/409/503 are treated as "still running" and retried with exponential
    backoff (0.5s start, capped at 2s) until opencode_serve_wait_timeout_s.
    401/404/400 and other 4xx/5xx raise ServeError immediately; exceeding the
    deadline raises WaitTimeoutError (mapped to 504 by the gateway).
    """
    url = f"/api/session/{session_id}/wait"
    deadline = time.monotonic() + settings.opencode_serve_wait_timeout_s
    delay = 0.5
    while True:
        resp = await client.post(url, json={})
        status = resp.status_code
        if status < 400:
            return
        if status == 401:
            raise ServeError(
                "opencode serve returned 401; check OPENCODE_SERVER_PASSWORD"
            )
        if status not in _NON_FATAL_WAIT_STATUSES:
            raise ServeError(
                f"opencode serve returned {status} for POST {url}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeoutError(
                "opencode serve wait exceeded "
                f"{settings.opencode_serve_wait_timeout_s}s "
                f"(last status {status})"
            )
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 2.0)


async def run_chat(
    settings: Settings, body: dict[str, Any], client: httpx.AsyncClient
) -> ServeResult:
    """Execute session->prompt->wait->message and return the final assistant turn."""
    provider, model_id = parse_model(body.get("model"))
    prompt = build_prompt(body["messages"])

    session = await _request(
        client,
        "POST",
        "/api/session",
        json={"model": {"id": model_id, "providerID": provider}},
    )
    session_id = (session or {}).get("data", {}).get("id")
    if not session_id and isinstance(session, dict):
        session_id = session.get("id")
    if not session_id:
        raise ServeError("opencode serve did not return a session id")

    await _request(
        client,
        "POST",
        f"/api/session/{session_id}/prompt",
        json={"prompt": {"text": prompt}, "model": {"id": model_id, "providerID": provider}},
    )
    await _poll_wait(client, session_id, settings)
    messages = await _request(client, "GET", f"/api/session/{session_id}/message")
    result = _extract_assistant(messages)
    result.session_id = str(session_id)
    return result
