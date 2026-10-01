#!/usr/bin/env python3
"""Protocol-level end-to-end test for the gateway.

Why this exists: `scripts/e2e_hermes.py` drives the gateway through a real
hermes install and a real upstream, which makes it slow, quota-dependent and
unusable in CI. This script instead stands up

    mock OpenAI upstream  ->  proxy_opencode gateway  ->  this test

on loopback and asserts the *whole* HTTP surface: all three protocols, both
streaming and non-streaming, model masking (including the upstream display
name Zen smuggles into `delta.name`), tool calling, the documented
client-tool-name collision trap, auth, upstream error relay and the API key
lifecycle.

The mock upstream deliberately behaves like Zen: chunks carry
`"model": "big-pickle"` (the bare id the live catalogue switched to on
2026-10-02) and a leaked `"name": "Space Bunny"`. A passing run therefore
proves the masking layer really scrubs both.

Usage:
    python scripts/e2e_protocols.py                 # mock upstream (default)
    python scripts/e2e_protocols.py --report out.json

Exit code 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_MODEL = "big-pickle"               # what the mock echoes as "model"
LEAKED_DISPLAY_NAME = "Space Bunny"          # what the mock smuggles in delta.name
PUBLIC_MODEL = "ds41f_cus"                   # what clients must see

GATEWAY_KEY = "e2e-gateway-key"
ADMIN_KEY = "e2e-admin-key"


# --------------------------------------------------------------- mock upstream


def _chat_response(body: dict[str, Any]) -> dict[str, Any]:
    """Non-streaming chat completion, shaped like Zen's."""
    wants_tool = bool(body.get("tools"))
    message: dict[str, Any] = {
        "role": "assistant",
        "content": None if wants_tool else "pong",
        "reasoning_content": "thinking about it",
        "name": LEAKED_DISPLAY_NAME,          # the leak under test
    }
    if wants_tool:
        message["tool_calls"] = [
            {
                "id": "call_e2e_1",
                "type": "function",
                "function": {
                    "name": str(
                        ((body["tools"][0] or {}).get("function") or {}).get("name")
                        or "probe"
                    ),
                    "arguments": '{"value": 42}',
                },
            }
        ]
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": UPSTREAM_MODEL,               # must be masked by the gateway
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if wants_tool else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 5,
            "total_tokens": 16,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    """One OpenAI chat-completion SSE chunk carrying `delta`."""
    payload = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": UPSTREAM_MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_chunks(body: dict[str, Any]) -> list[bytes]:
    """Streaming chunks shaped like Zen's, leak included."""
    wants_tool = bool(body.get("tools"))
    # First chunk: role + reasoning + the leaked display name.
    out = [
        _chunk(
            {
                "role": "assistant",
                "reasoning_content": "thinking",
                "name": LEAKED_DISPLAY_NAME,
            }
        )
    ]
    if wants_tool:
        name = str(
            ((body["tools"][0] or {}).get("function") or {}).get("name") or "probe"
        )
        out.append(
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_e2e_1",
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
            )
        )
        out.append(
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"value": 42}'}}]})
        )
        finish = "tool_calls"
    else:
        out.append(_chunk({"content": "po"}))
        out.append(_chunk({"content": "ng"}))
        finish = "stop"
    out.append(_chunk({}, finish))
    usage = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": UPSTREAM_MODEL,
        "choices": [],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 5,
            "total_tokens": 16,
        },
    }
    out.append(f"data: {json.dumps(usage)}\n\n".encode())
    out.append(b"data: [DONE]\n\n")
    return out


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silence
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/v1/models"):
            self._json(200, {"object": "list", "data": [{"id": UPSTREAM_MODEL}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if not self.path.endswith("/v1/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        # Error-relay probe: a 429 must reach the client with its status intact.
        if body.get("model") == "trigger-429" or any(
            "trigger-429" in str(m.get("content") or "") for m in body.get("messages") or []
        ):
            self._json(
                429,
                {
                    "type": "error",
                    "error": {
                        "type": "FreeUsageLimitError",
                        "message": "Rate limit exceeded. Please try again later.",
                    },
                },
            )
            return
        if not body.get("stream"):
            self._json(200, _chat_response(body))
            return
        chunks = _sse_chunks(body)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()


class _MockServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that swallows client-disconnect noise.

    The mock answers streamed responses with `connection: close`, so the
    gateway (or httpx) tearing the socket down surfaces as a
    `ConnectionResetError` inside `handle_one_request`. That is expected and
    printing a traceback for it only obscures real failures.
    """

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class MockUpstream:
    def __init__(self) -> None:
        self.port = _free_port()
        self.server = _MockServer(("127.0.0.1", self.port), _MockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ------------------------------------------------------------------- plumbing


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Gateway:
    """Runs `python -m proxy_opencode` against the mock upstream."""

    def __init__(self, upstream_base: str, key_store: Path) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = key_store.parent / "gateway.log"
        env = {
            **os.environ,
            "UPSTREAM_MODE": "openai",
            "UPSTREAM_BASE_URL": upstream_base,
            "UPSTREAM_API_KEY": "mock-upstream-key",
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "GATEWAY_API_KEYS": GATEWAY_KEY,
            "ADMIN_API_KEYS": ADMIN_KEY,
            "KEY_STORE_PATH": str(key_store),
            "LOG_FORMAT": "text",
            "PUBLIC_MODELS": f"{PUBLIC_MODEL}:DeepSeek V4.1 Flash:{UPSTREAM_MODEL}",
        }
        self._log = open(self.log_path, "wb")  # noqa: SIM115 - closed in stop()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "proxy_opencode"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 25.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"gateway exited early (rc={self.proc.returncode}); see {self.log_path}"
                )
            try:
                if httpx.get(f"{self.base_url}/healthz", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError(f"gateway did not become ready; see {self.log_path}")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail[:400]})
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
        return ok

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [r for r in self.results if not r["ok"]]


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into (event_name, payload) pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        out.append((name or str(payload.get("type") or ""), payload))
    return out


def _chat_stream_content(text: str) -> str:
    """Reassemble assistant text from an OpenAI chat SSE body."""
    parts: list[str] = []
    for _name, payload in _sse_events(text):
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(str(delta["content"]))
    return "".join(parts)


# --------------------------------------------------------------------- checks


def run_checks(gw: Gateway) -> Checks:
    c = Checks()
    auth = {"Authorization": f"Bearer {GATEWAY_KEY}"}
    base = gw.base_url

    with httpx.Client(base_url=base, timeout=60.0) as client:
        # ---- healthz / models
        h = client.get("/healthz")
        c.record(
            "healthz reports version + mode",
            h.status_code == 200 and h.json().get("upstream_mode") == "openai",
            str(h.text[:120]),
        )
        m = client.get("/v1/models", headers=auth)
        body = m.text
        c.record(
            "GET /v1/models exposes only the public catalogue",
            m.status_code == 200
            and PUBLIC_MODEL in body
            and UPSTREAM_MODEL not in body,
            body[:160],
        )

        # ---- auth
        r = client.post(
            "/v1/chat/completions",
            json={"model": PUBLIC_MODEL, "messages": [{"role": "user", "content": "hi"}]},
        )
        c.record("missing key -> 401", r.status_code == 401, str(r.status_code))
        r = client.post(
            "/v1/chat/completions",
            json={"model": PUBLIC_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer wrong"},
        )
        c.record("wrong key -> 401", r.status_code == 401, str(r.status_code))

        # ---- chat non-stream
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": PUBLIC_MODEL,
                "stream": False,
                "messages": [{"role": "user", "content": "say pong"}],
            },
            headers=auth,
        )
        text = r.text
        payload = r.json() if r.status_code == 200 else {}
        c.record(
            "chat (non-stream) returns content",
            r.status_code == 200
            and payload.get("choices", [{}])[0].get("message", {}).get("content") == "pong",
            text[:200],
        )
        c.record(
            "chat (non-stream) masks model + drops leaked display name",
            payload.get("model") == PUBLIC_MODEL
            and UPSTREAM_MODEL not in text
            and LEAKED_DISPLAY_NAME not in text,
            text[:240],
        )
        c.record(
            "chat (non-stream) forwards usage",
            (payload.get("usage") or {}).get("prompt_tokens") == 11,
            str(payload.get("usage")),
        )

        # ---- chat stream
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": PUBLIC_MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": "say pong"}],
            },
            headers=auth,
        ) as resp:
            stream_text = "".join(resp.iter_text())
            status = resp.status_code
        c.record(
            "chat (stream) relays SSE and terminates with [DONE]",
            status == 200
            and "[DONE]" in stream_text
            and _chat_stream_content(stream_text) == "pong",
            f"content={_chat_stream_content(stream_text)!r}",
        )
        c.record(
            "chat (stream) masks model + leaked name in EVERY chunk",
            UPSTREAM_MODEL not in stream_text
            and LEAKED_DISPLAY_NAME not in stream_text
            and PUBLIC_MODEL in stream_text,
            stream_text[:240],
        )
        # A tool-bearing stream: `function.name` must survive the scrub that
        # removes the leaked `delta.name`, otherwise clients lose the tool.
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": PUBLIC_MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "probe",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
            headers=auth,
        ) as resp:
            tool_stream_text = "".join(resp.iter_text())
            tool_stream_status = resp.status_code
        streamed_names = [
            fn["name"]
            for _n, payload in _sse_events(tool_stream_text)
            for choice in payload.get("choices") or []
            for call in (choice.get("delta") or {}).get("tool_calls") or []
            if (fn := call.get("function")) and fn.get("name")
        ]
        c.record(
            "chat (stream) preserves tool function names",
            tool_stream_status == 200
            and streamed_names == ["probe"]
            and LEAKED_DISPLAY_NAME not in tool_stream_text
            and UPSTREAM_MODEL not in tool_stream_text,
            f"names={streamed_names!r} status={tool_stream_status}",
        )

        # ---- tool calling + the documented client-name collision trap
        for tool_name in ("probe", "bash"):
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": PUBLIC_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": "call the tool"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "description": "probe",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                },
                headers=auth,
            )
            data = r.json() if r.status_code == 200 else {}
            calls = (
                (data.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
            )
            c.record(
                f"tool call surfaced for client tool {tool_name!r}",
                r.status_code == 200 and calls and calls[0]["function"]["name"] == tool_name,
                r.text[:200],
            )

        # ---- upstream error relay
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": PUBLIC_MODEL,
                "stream": False,
                "messages": [{"role": "user", "content": "trigger-429"}],
            },
            headers=auth,
        )
        c.record(
            "upstream 429 relayed with its original status",
            r.status_code == 429 and "FreeUsageLimitError" in r.text,
            f"{r.status_code} {r.text[:160]}",
        )

        # ---- responses API
        r = client.post(
            "/v1/responses",
            json={
                "model": PUBLIC_MODEL,
                "input": "say pong",
                "instructions": "be brief",
                "stream": False,
            },
            headers=auth,
        )
        data = r.json() if r.status_code == 200 else {}
        c.record(
            "responses (non-stream) returns a response envelope",
            r.status_code == 200
            and data.get("object") == "response"
            and data.get("status") == "completed"
            and data.get("model") == PUBLIC_MODEL
            and data.get("output"),
            r.text[:220],
        )
        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": PUBLIC_MODEL, "input": "say pong", "stream": True},
            headers=auth,
        ) as resp:
            rs_text = "".join(resp.iter_text())
            rs_status = resp.status_code
        rs_events = _sse_events(rs_text)
        rs_names = [n for n, _ in rs_events]
        c.record(
            "responses (stream) emits the canonical event sequence",
            rs_status == 200
            and rs_names[:1] == ["response.created"]
            and rs_names[-1:] == ["response.completed"]
            and "response.output_item.added" in rs_names,
            str(rs_names[:8]),
        )
        rs_added = [
            p.get("output_index") for n, p in rs_events if n == "response.output_item.added"
        ]
        c.record(
            "responses (stream) output_index values are unique",
            bool(rs_added) and len(set(rs_added)) == len(rs_added),
            f"indices={rs_added}",
        )
        c.record(
            "responses (stream) does not leak the upstream model",
            UPSTREAM_MODEL not in rs_text and LEAKED_DISPLAY_NAME not in rs_text,
            rs_text[:160],
        )

        # ---- anthropic messages API
        r = client.post(
            "/v1/messages",
            json={
                "model": PUBLIC_MODEL,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "say pong"}],
            },
            headers={"x-api-key": GATEWAY_KEY},
        )
        data = r.json() if r.status_code == 200 else {}
        c.record(
            "messages (non-stream) returns an Anthropic envelope",
            r.status_code == 200
            and data.get("type") == "message"
            and data.get("role") == "assistant"
            and data.get("model") == PUBLIC_MODEL
            and data.get("content"),
            r.text[:220],
        )
        c.record(
            "messages (non-stream) reports input+output tokens",
            (data.get("usage") or {}).get("input_tokens") == 11
            and (data.get("usage") or {}).get("output_tokens") == 5,
            str(data.get("usage")),
        )
        with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": PUBLIC_MODEL,
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "say pong"}],
            },
            headers={"x-api-key": GATEWAY_KEY},
        ) as resp:
            an_text = "".join(resp.iter_text())
            an_status = resp.status_code
        an_events = _sse_events(an_text)
        an_names = [n for n, _ in an_events]
        an_delta = [p for n, p in an_events if n == "message_delta"]
        c.record(
            "messages (stream) emits the canonical event sequence",
            an_status == 200
            and an_names[:1] == ["message_start"]
            and an_names[-1:] == ["message_stop"]
            and "content_block_delta" in an_names,
            str(an_names[:8]),
        )
        c.record(
            "messages (stream) every data body carries its own type",
            all(p.get("type") == n for n, p in an_events),
            str([(n, p.get("type")) for n, p in an_events[:3]]),
        )
        c.record(
            "messages (stream) message_delta reports real input_tokens",
            bool(an_delta) and an_delta[0]["usage"].get("input_tokens") == 11,
            str(an_delta[0]["usage"]) if an_delta else "no message_delta",
        )
        c.record(
            "messages (stream) does not leak the upstream model",
            UPSTREAM_MODEL not in an_text and LEAKED_DISPLAY_NAME not in an_text,
            an_text[:160],
        )

        # ---- key lifecycle
        minted = client.post(
            "/admin/keys",
            json={"name": "e2e", "ttl_hours": 24},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        rec = minted.json() if minted.status_code == 200 else {}
        c.record(
            "POST /admin/keys mints a key",
            minted.status_code == 200 and str(rec.get("key", "")).startswith("gw-"),
            minted.text[:160],
        )
        if rec.get("key"):
            r = client.post(
                "/v1/chat/completions",
                json={"model": PUBLIC_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {rec['key']}"},
            )
            c.record("minted key authenticates", r.status_code == 200, str(r.status_code))
        listing = client.get("/admin/keys", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        c.record(
            "GET /admin/keys masks key material",
            listing.status_code == 200
            and str(rec.get("key", "x")) not in listing.text
            and "key_preview" in listing.text,
            listing.text[:160],
        )
        r = client.post(
            "/admin/keys", json={}, headers={"Authorization": f"Bearer {GATEWAY_KEY}"}
        )
        c.record("non-admin key cannot mint keys", r.status_code == 401, str(r.status_code))
        bad = client.post(
            "/admin/keys",
            json={"ttl_hours": "nope"},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        c.record("invalid ttl_hours -> 400", bad.status_code == 400, str(bad.status_code))
        if rec.get("id"):
            rev = client.delete(
                f"/admin/keys/{rec['id']}", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
            )
            c.record("DELETE /admin/keys revokes", rev.status_code == 200, str(rev.status_code))
            r = client.post(
                "/v1/chat/completions",
                json={"model": PUBLIC_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {rec['key']}"},
            )
            c.record("revoked key -> 401", r.status_code == 401, str(r.status_code))
        missing = client.delete(
            "/admin/keys/k-nope", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        c.record("unknown key id -> 404", missing.status_code == 404, str(missing.status_code))

        # ---- unmasked direct upstream id must still be masked back
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": UPSTREAM_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth,
        )
        c.record(
            "requesting the upstream id directly is masked in the response",
            r.status_code == 200 and r.json().get("model") == PUBLIC_MODEL,
            r.text[:160],
        )
    return c


# ----------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="", help="write a JSON report here")
    args = parser.parse_args()

    workdir = REPO_ROOT / ".e2e_tmp"
    workdir.mkdir(exist_ok=True)
    key_store = workdir / "keys.json"
    key_store.unlink(missing_ok=True)

    upstream = MockUpstream()
    upstream.start()
    gw = Gateway(upstream.base_url, key_store)
    print(f"mock upstream : {upstream.base_url}")
    print(f"gateway       : {gw.base_url}")
    try:
        gw.wait_ready()
        checks = run_checks(gw)
    finally:
        gw.stop()
        upstream.stop()

    total = len(checks.results)
    failed = checks.failures
    print(f"\n{'=' * 60}\nE2E: {total - len(failed)}/{total} checks passed")
    if failed:
        print("FAILED:")
        for item in failed:
            print(f"  - {item['check']}: {item['detail']}")
    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {
                    "total": total,
                    "passed": total - len(failed),
                    "checks": checks.results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"report: {args.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
