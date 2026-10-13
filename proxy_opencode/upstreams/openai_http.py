"""Plain OpenAI-compatible HTTP passthrough adapter (UPSTREAM_MODE=openai).

Forwards whitelisted chat payloads to UPSTREAM_BASE_URL with
UPSTREAM_API_KEY. Streaming responses are relayed byte-for-byte.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any, AsyncIterator

import httpx

from ..config import Settings

_CHUNK_ID_RE = re.compile(rb'"id":"([^"]+)"')


class StreamRelay:
    """Wraps an upstream streaming response for the route layer to consume.

    Upstream providers behind Zen intermittently drop streams mid-generation:
    the connection closes cleanly after content chunks but WITHOUT the
    terminal ``finish_reason`` chunk, the usage chunk and ``data: [DONE]``
    (live-verified 2026-10-13 at ~25-30% rate for long generations; the
    client sees a stream that just stops). OpenAI-wire clients classify that
    as a protocol violation and hard-fail (hermes: "Response remained
    truncated after 4 continuation attempts"). To keep the wire contract
    honest and explicit, :meth:`chunks` synthesises a terminal
    ``finish_reason: "length"`` chunk + ``[DONE]`` when the upstream stream
    ends without one, so a silent cut surfaces as a normal output-cap
    truncation that clients already know how to handle.
    """

    def __init__(self, resp: httpx.Response, usage_holder: dict[str, Any]) -> None:
        self.response = resp
        self.usage_holder = usage_holder
        # Set after chunks() ran: True when the terminal chunk was synthesised.
        self.stream_cut = False

    async def chunks(self) -> AsyncIterator[bytes]:
        resp = self.response
        # Best-effort capture of usage from streamed chunks for logging. The
        # scan is line-buffered: a `data:` line carrying usage can be split
        # across any number of network chunks, and a naive per-chunk
        # `json.loads` silently drops it (observed with long tool-call
        # streams). Bytes are still relayed verbatim — only the parse is
        # buffered.
        pending = b""
        saw_finish = False
        saw_done = False
        last_id = b""

        def scan_line(line: bytes) -> None:
            nonlocal saw_finish, saw_done, last_id
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                return
            payload = stripped[5:].strip()
            if payload == b"[DONE]":
                saw_done = True
                return
            if b'"id":"' in stripped:
                match = _CHUNK_ID_RE.search(stripped)
                if match:
                    last_id = match.group(1)
            if b"usage" in stripped:
                try:
                    data = json.loads(payload)
                except Exception:
                    data = None
                if isinstance(data, dict) and data.get("usage"):
                    self.usage_holder["usage"] = data["usage"]
            # A terminal chunk carries a non-null finish_reason. Zen emits
            # compact JSON (`"finish_reason":"stop"` / `"finish_reason":null`),
            # so the quoted-value form is a conservative signal: missing a
            # real finish chunk would only duplicate a harmless terminator,
            # while a false positive would relabel a completed stream.
            if b'"finish_reason":"' in stripped or b'"finish_reason": "' in stripped:
                saw_finish = True

        try:
            async for chunk in resp.aiter_bytes():
                # Always run the line-buffered scan (cheap bytes ops): id /
                # finish markers live on lines that carry neither "usage" nor
                # "finish_reason", and a skipped line means a missed terminal
                # chunk or a lost completion id for the synthesised tail.
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    scan_line(line)
                yield chunk
            if pending:
                scan_line(pending)
        finally:
            await resp.aclose()
        if not saw_finish:
            # Silent upstream cut: make the truncation explicit so clients
            # treat it as an output-cap stop instead of a protocol violation.
            self.stream_cut = True
            completion_id = last_id.decode("utf-8", "replace") if last_id else (
                f"chatcmpl-zen-{secrets.token_hex(8)}"
            )
            synth = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            }
            yield f"data: {json.dumps(synth, separators=(',', ':'))}\n\n".encode()
        if not saw_done:
            yield b"data: [DONE]\n\n"


class OpenAIHttpAdapter:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=httpx.Timeout(300.0, connect=30.0),
            headers={
                "Authorization": f"Bearer {settings.upstream_api_key}",
                "Content-Type": "application/json",
            },
            # Never follow ambient proxy settings (see upstream_trust_env).
            trust_env=settings.upstream_trust_env,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def list_models(self) -> dict[str, Any] | httpx.Response:
        resp = await self.client.get("/v1/models")
        if resp.status_code != 200:
            return resp
        return resp.json()

    async def chat(self, payload: dict[str, Any]) -> Any:
        """Returns a JSON body, an httpx.Response (error relay), or a StreamRelay."""
        stream = bool(payload.get("stream"))
        if stream:
            req = self.client.build_request(
                "POST", "/v1/chat/completions", json=payload
            )
            try:
                resp = await self.client.send(req, stream=True)
            except httpx.HTTPError as exc:
                raise ConnectionError(f"Upstream request failed: {exc}") from exc
            if resp.status_code != 200:
                body = await resp.aread()
                await resp.aclose()
                return _raw_relay_body(resp.status_code, body)
            return StreamRelay(resp, {})

        try:
            resp = await self.client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ConnectionError(f"Upstream request failed: {exc}") from exc
        if resp.status_code != 200:
            return _raw_relay_body(resp.status_code, resp.content)
        try:
            return resp.json()
        except Exception:
            return _raw_relay_body(502, b"")


def _raw_relay_body(status: int, body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except Exception:
        data = None
    return {
        "__status__": status,
        "content": data
        if isinstance(data, dict)
        else {
            "error": {
                "message": f"Upstream error ({status}).",
                "type": "api_error",
                "param": None,
                "code": None,
            }
        },
    }
