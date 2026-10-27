"""Shared response assembly for the three protocol routes.

``/v1/chat/completions``, ``/v1/responses`` and ``/v1/messages`` differ only in
how they convert payloads and shape bodies. The *transport* wiring around a
result is identical and used to be copy-pasted three times — which is exactly
where the drift started (one route forgot the fallback note, another skipped
the stream-lifecycle record).

Three helpers cover the shared part:

* :func:`begin` — capture the fallback provenance (headers + SSE note) and fold
  it into the request's log fields, once, before the response starts;
* :func:`streaming` — wrap an already-converted SSE byte stream in the
  lifecycle-logging relay (stream watchdog + "stream ended" record);
* :func:`json_response` — log the completion record and answer with the
  fallback headers attached.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from ..config import Settings
from ..upstreams.failover import fallback_headers, fallback_note
from ._pipeline import ChatCall, fallback_log_fields, relay_stream

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def begin(call: ChatCall) -> tuple[dict[str, str], str]:
    """Capture this request's fallback provenance.

    Must be called before the response starts: the contextvar marker lives for
    one request only. Returns ``(headers, sse_note)`` and records the fallback
    fields on the call so the completion log line carries them.
    """

    headers = fallback_headers()
    note = fallback_note()
    call.extra_log_fields.update(fallback_log_fields())
    return headers, note


def wants_fallback_note(note: str) -> bool:
    """True when a streamed answer must announce the backup chain."""

    return bool(note)


def streaming(
    chunks: AsyncIterator[bytes],
    usage_holder: dict[str, Any],
    call: ChatCall,
    settings: Settings,
    logger: logging.Logger,
    *,
    extra_headers: dict[str, str],
) -> StreamingResponse:
    """Serve a converted SSE stream through the shared lifecycle relay."""

    return StreamingResponse(
        relay_stream(
            chunks,
            usage_holder,
            lambda status, usage=None: call.log(logger, status, usage),
            request_id=call.request_id,
            model=call.upstream_model,
            client=call.client,
            client_stall_s=settings.client_stall_s,
        ),
        status_code=200,
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **extra_headers},
    )


def json_response(
    payload: dict[str, Any],
    call: ChatCall,
    logger: logging.Logger,
    *,
    extra_headers: dict[str, str],
    usage: Any = None,
) -> JSONResponse:
    """Answer a completed turn with a JSON body (logs exactly one record).

    ``usage`` overrides the value read from the body — the Responses and
    Anthropic envelopes carry their own usage shape, but the completion record
    must keep reporting the chat-shaped counters.
    """

    call.log(logger, 200, payload.get("usage") if usage is None else usage)
    return JSONResponse(status_code=200, content=payload, headers=extra_headers)


__all__ = [
    "SSE_HEADERS",
    "begin",
    "json_response",
    "streaming",
    "wants_fallback_note",
]
