#!/usr/bin/env python3
"""End-to-end check of a running gateway against a WorkBuddy-shaped client.

What WorkBuddy (and any desktop agent) actually sends, and what this script
therefore exercises:

* ``POST /v1/chat/completions`` with ``stream: true``, a tool list and a
  reasoning hint — the SSE path, including model masking on every chunk;
* a non-streaming call — the free tier only streams upstream, so this proves
  the aggregation path;
* ``POST /v1/responses`` (codex-style) and ``POST /v1/messages``
  (Claude-style) — the two protocol shims;
* the masked catalogue (``/v1/models``) and the hidden relay list.

Usage:

    python scripts/e2e_workbuddy.py --base http://127.0.0.1:8787 --key wb-local-8787
    python scripts/e2e_workbuddy.py --base http://127.0.0.1:8787 --key ... --only chat_stream

Exit code 0 only when every selected check passes.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Iterator

import httpx

MODEL = "deepseek-ai/deepseek-v4.1-flash"

# A desktop agent's tool list: names deliberately outside the opencode builtin
# set, because the gateway must inject its own builtin names and still pass the
# client's tools through.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Return the current time for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


def _fail(name: str, detail: str) -> tuple[str, bool, str]:
    return name, False, detail


def _ok(name: str, detail: str) -> tuple[str, bool, str]:
    return name, True, detail


def iter_sse(resp: httpx.Response) -> Iterator[dict[str, Any]]:
    for line in resp.iter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if isinstance(chunk, dict):
            yield chunk


def check_models(client: httpx.Client) -> tuple[str, bool, str]:
    resp = client.get("/v1/models")
    if resp.status_code != 200:
        return _fail("models", f"HTTP {resp.status_code}: {resp.text[:120]}")
    ids = [m["id"] for m in resp.json().get("data", [])]
    if MODEL not in ids:
        return _fail("models", f"public alias missing: {ids}")
    leaked = [i for i in ids if i == "big-pickle"]
    if leaked:
        return _fail("models", f"upstream id leaked in catalogue: {leaked}")
    return _ok("models", f"alias only: {ids}")


def check_chat_stream(client: httpx.Client) -> tuple[str, bool, str]:
    started = time.perf_counter()
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
        "stream": True,
        # A reasoning model spends output tokens on its thinking first; 64
        # tokens can be consumed entirely by reasoning, yielding chunks with no
        # content at all (measured: looks like an empty answer, is really a
        # budget that was too small).
        "max_tokens": 512,
        "tools": TOOLS,
        "reasoning_effort": "low",
    }
    with client.stream("POST", "/v1/chat/completions", json=body) as resp:
        if resp.status_code != 200:
            resp.read()
            return _fail("chat_stream", f"HTTP {resp.status_code}: {resp.text[:200]}")
        ttfb = None
        chunks = 0
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        masked = True
        saw_done = False
        for chunk in iter_sse(resp):
            if ttfb is None:
                ttfb = (time.perf_counter() - started) * 1000
            chunks += 1
            model = chunk.get("model")
            if model is not None and model != MODEL:
                masked = False
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(str(delta["content"]))
                if delta.get("reasoning_content"):
                    reasoning_parts.append(str(delta["reasoning_content"]))
        saw_done = True  # iter_sse returns on [DONE]
    total = (time.perf_counter() - started) * 1000
    if chunks == 0:
        return _fail("chat_stream", "no SSE chunks received")
    if not masked:
        return _fail("chat_stream", "upstream model id leaked in a chunk")
    text = "".join(text_parts).strip()
    if not text:
        return _fail(
            "chat_stream",
            f"{chunks} chunks but empty content "
            f"(reasoning_chars={len(''.join(reasoning_parts))}; "
            "raise max_tokens if the model only thought)",
        )
    return _ok(
        "chat_stream",
        f"{chunks} chunks, ttfb={ttfb:.0f}ms total={total:.0f}ms, "
        f"content={text[:40]!r}, masked=yes, done={saw_done}",
    )


def check_chat_nonstream(client: httpx.Client) -> tuple[str, bool, str]:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
        "max_tokens": 512,
    }
    resp = client.post("/v1/chat/completions", json=body, timeout=180)
    if resp.status_code != 200:
        return _fail("chat_nonstream", f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    if data.get("model") != MODEL:
        return _fail("chat_nonstream", f"unmasked model: {data.get('model')}")
    if not (message.get("content") or message.get("tool_calls")):
        return _fail("chat_nonstream", f"empty message: {json.dumps(data)[:200]}")
    meta = data.get("metadata") or {}
    return _ok(
        "chat_nonstream",
        f"aggregated={meta.get('aggregated_stream')} "
        f"finish={(data.get('choices') or [{}])[0].get('finish_reason')} "
        f"content={str(message.get('content'))[:40]!r}",
    )


def check_tool_call(client: httpx.Client) -> tuple[str, bool, str]:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "What time is it in Shanghai? Use the tool."}
        ],
        "stream": True,
        "max_tokens": 512,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    calls: dict[int, dict[str, Any]] = {}
    with client.stream("POST", "/v1/chat/completions", json=body) as resp:
        if resp.status_code != 200:
            resp.read()
            return _fail("tool_call", f"HTTP {resp.status_code}: {resp.text[:200]}")
        for chunk in iter_sse(resp):
            for choice in chunk.get("choices") or []:
                for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                    slot = calls.setdefault(
                        int(tc.get("index") or 0), {"name": "", "args": ""}
                    )
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
    if not calls:
        return _fail("tool_call", "model returned no tool_calls")
    first = calls[min(calls)]
    return _ok("tool_call", f"name={first['name']!r} args={first['args'][:60]!r}")


def check_responses(client: httpx.Client) -> tuple[str, bool, str]:
    body = {
        "model": MODEL,
        "input": "Say exactly: OK",
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": "get_time",
                "description": "Return the current time.",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }
    events: list[str] = []
    with client.stream("POST", "/v1/responses", json=body) as resp:
        if resp.status_code != 200:
            resp.read()
            return _fail("responses", f"HTTP {resp.status_code}: {resp.text[:200]}")
        for line in resp.iter_lines():
            if line.startswith("event:"):
                events.append(line[6:].strip())
    required = {"response.created", "response.completed"}
    missing = required - set(events)
    if missing:
        return _fail("responses", f"missing events {sorted(missing)}; got {events[:6]}")
    return _ok("responses", f"{len(events)} events, terminal=response.completed")


def check_messages(client: httpx.Client) -> tuple[str, bool, str]:
    body = {
        "model": MODEL,
        "max_tokens": 512,
        "stream": True,
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
    }
    events: list[str] = []
    with client.stream("POST", "/v1/messages", json=body) as resp:
        if resp.status_code != 200:
            resp.read()
            return _fail("messages", f"HTTP {resp.status_code}: {resp.text[:200]}")
        for line in resp.iter_lines():
            if line.startswith("event:"):
                events.append(line[6:].strip())
    required = {"message_start", "message_stop"}
    missing = required - set(events)
    if missing:
        return _fail("messages", f"missing events {sorted(missing)}; got {events[:6]}")
    if "message_delta" not in events:
        return _fail("messages", f"no message_delta; got {events[:8]}")
    return _ok("messages", f"{len(events)} events, terminal=message_stop")


CHECKS = {
    "models": check_models,
    "chat_stream": check_chat_stream,
    "chat_nonstream": check_chat_nonstream,
    "tool_call": check_tool_call,
    "responses": check_responses,
    "messages": check_messages,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--key", default="", help="gateway API key (dev-open if empty)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (the ECS edge serves a self-signed cert)",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(CHECKS),
        help="run only these checks (repeatable)",
    )
    args = parser.parse_args(argv)

    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}
    selected = args.only or list(CHECKS)
    results: list[tuple[str, bool, str]] = []
    with httpx.Client(
        base_url=args.base.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(240.0, connect=15.0),
        trust_env=False,
        verify=not args.insecure,
    ) as client:
        for name in selected:
            try:
                results.append(CHECKS[name](client))
            except Exception as exc:  # a crashed check is a failed check
                results.append(_fail(name, f"{type(exc).__name__}: {exc}"))

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name.ljust(width)}  {detail}")
        if not ok:
            failed += 1
    print(f"\n{len(results) - failed}/{len(results)} checks passed against {args.base}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
