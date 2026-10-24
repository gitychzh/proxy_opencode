"""Plain OpenAI-compatible HTTP passthrough adapter (UPSTREAM_MODE=openai).

Forwards whitelisted chat payloads to UPSTREAM_BASE_URL with
UPSTREAM_API_KEY. Streaming responses are relayed byte-for-byte.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Protocol

import httpx

from ..config import Settings

logger = logging.getLogger("proxy_opencode.stream")

_CHUNK_ID_RE = re.compile(rb'"id":"([^"]+)"')

# Stream end outcomes, passed to the close hook exactly once when the relay
# finishes, however it finishes:
#   "completed"    — upstream sent a terminal finish_reason chunk
#   "cut"          — upstream closed cleanly without a terminal chunk
#   "stalled"      — no body byte within the stall watchdog window
#   "interrupted"  — mid-stream transport error (read error, reset, ...)
#   "disconnected" — the consumer closed the generator (client hung up)
CloseHook = Callable[[str], Awaitable[None]]


class ResponseLike(Protocol):
    """Minimal upstream-response surface the relay/aggregator consumes.

    ``httpx.Response`` satisfies it natively; the bun transport facade
    (``upstreams/zen_bun_transport.py``, the 2026-10-24 stack-gate hop)
    implements the same duck surface for its subprocess streams.
    """

    status_code: int
    headers: httpx.Headers

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...

    def aiter_lines(self) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


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

    Second failure mode (found 2026-10-19 while diagnosing a hermes
    ``stream_drop``): the upstream does not close the stream at all — a
    dead egress tunnel leaves the connection open with ZERO bytes until
    the 300s read timeout, after which the exception tears the client
    connection down with no terminator (hermes: "The connection dropped
    before the reply finished."). A stall watchdog (``stall_s``, from
    ``STREAM_STALL_S``) bounds the silence — both for the first body byte
    after the headers and between bytes — and then stops waiting, letting
    the same synthesised-terminator path close the wire cleanly; the
    outcome is handed to the close hook so an egress pool can release the
    lease as a failure and cordon the dead bucket. ``stall_s <= 0``
    disables the watchdog (legacy behaviour: silence is only bounded by
    the client read timeout and the cut surfaces as a hard drop).
    """

    def __init__(
        self,
        resp: ResponseLike,
        usage_holder: dict[str, Any],
        on_close: CloseHook | None = None,
        *,
        stall_s: float = 0.0,
    ) -> None:
        self.response = resp
        self.usage_holder = usage_holder
        # Set after chunks() ran: True when the terminal chunk was synthesised.
        self.stream_cut = False
        # Final outcome of _stream() (see CloseHook): None until it ends;
        # still None at hook time means the consumer closed the generator
        # mid-stream, i.e. the client disconnected.
        self.end_reason: str | None = None
        # Max tolerated silence between body bytes; <= 0 disables the watchdog.
        self._stall_s = float(stall_s)
        # Called once when the relay finishes (the egress pool releases its
        # lease then — in-flight accounting must span the whole stream).
        self._on_close: CloseHook | None = on_close

    async def chunks(self) -> AsyncIterator[bytes]:
        """Relay the upstream SSE body, running ``on_close`` exactly once.

        The close hook must fire even when the consumer abandons the stream
        (client disconnect): the route layer closes this generator explicitly,
        which raises ``GeneratorExit`` at the current ``yield``. We therefore
        close the inner producer deterministically and then run the hook, so
        the egress lease is released the moment the stream ends rather than
        whenever the garbage collector gets to it.
        """
        inner = self._stream()
        try:
            async for chunk in inner:
                yield chunk
        finally:
            try:
                await inner.aclose()
            finally:
                await self._close()

    async def _close(self) -> None:
        hook = self._on_close
        if hook is None:
            return
        self._on_close = None
        try:
            # None => chunks() was closed before _stream() classified its
            # end: the consumer hung up (client disconnect).
            await hook(self.end_reason or "disconnected")
        except Exception:  # never let bookkeeping break the relay teardown
            logger.exception("stream relay close hook failed")

    async def _stream(self) -> AsyncGenerator[bytes, None]:
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
            iterator = resp.aiter_bytes()
            last_byte = time.monotonic()
            while True:
                try:
                    if self._stall_s > 0:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(), timeout=self._stall_s
                        )
                    else:
                        chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    # Stall watchdog: no byte for stall_s — either the first
                    # body byte after the headers or the next one mid-stream.
                    # A dead egress tunnel hangs exactly like this until the
                    # read timeout (measured 2026-10-19: 300s of silence, then
                    # the exception cut the client connection with no
                    # terminator). Stop waiting and fall through to the
                    # synthesised terminator instead.
                    logger.warning(
                        "upstream stream stalled",
                        extra={
                            "reason": "stream_stalled",
                            "detail": (
                                f"no bytes for {time.monotonic() - last_byte:.1f}s "
                                f"(limit {self._stall_s:g}s)"
                            ),
                        },
                    )
                    self.end_reason = "stalled"
                    break
                except httpx.HTTPError as exc:
                    # Mid-stream transport failure: classify it like a stall
                    # so the wire still ends with a synthesised terminator
                    # instead of a cut connection. Never any content here —
                    # only the error class and truncated message.
                    logger.warning(
                        "upstream stream interrupted",
                        extra={
                            "reason": "stream_interrupted",
                            "error": f"{type(exc).__name__}: {exc}"[:200],
                        },
                    )
                    self.end_reason = "interrupted"
                    break
                last_byte = time.monotonic()
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
        # Classify a normal end BEFORE synthesising: the yields below can
        # raise GeneratorExit (client disconnect) and end_reason must then
        # stay None so the close hook reports "disconnected".
        if self.end_reason is None:
            self.end_reason = "completed" if saw_finish else "cut"
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
            return StreamRelay(resp, {}, stall_s=self._settings.stream_stall_s)

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
