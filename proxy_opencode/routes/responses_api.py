"""POST /v1/responses — OpenAI Responses API (codex CLI compatible).

A thin protocol shim: converts the Responses request into an OpenAI chat
payload, feeds it through the shared adapter pipeline, then converts the
chat response (JSON body or SSE chunk stream) back into Responses format.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth import build_auth_dependency
from ..auth.keystore import KeyStore
from ..config import Settings
from ..errors import openai_error
from ..formats.responses_proto import (
    chat_to_responses_body,
    stream_responses_events,
    to_chat_payload,
)
from ..registry import ModelRegistry
from ..sse_mask import mask_json_model
from ..upstreams.openai_http import StreamRelay
from ..upstreams.opencode_serve import ServeCompletion, completion_to_chat_response
from ..upstreams.zen.tools import restore_map
from ._pipeline import (
    dispatch,
    is_error_relay,
    new_call,
    openai_error_factory,
    parse_json_object,
    with_fallback_note,
)
from ._render import begin, json_response, streaming

logger = logging.getLogger("proxy_opencode.responses")

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def make_router(
    settings: Settings, keystore: KeyStore, registry: ModelRegistry
) -> APIRouter:
    auth = build_auth_dependency(settings, keystore)
    router = APIRouter()

    @router.post("/v1/responses")
    async def handler(request: Request, _: str = Depends(auth)) -> Any:
        body = await parse_json_object(request, settings.payload_dump_dir)
        if body is None:
            return openai_error(400, "Request body must be a valid JSON object.")

        try:
            payload = to_chat_payload(body)
        except Exception:
            return openai_error(
                400, "Malformed Responses request.", err_type="invalid_request_error"
            )

        call = new_call(
            request, payload, settings, registry, log_message="responses completion"
        )
        adapter = request.app.state.adapter
        result = await dispatch(
            adapter,
            call,
            openai_error_factory,
            logger=logger,
            unhandled_message="unhandled responses error",
        )
        if isinstance(result, JSONResponse):
            return result

        # Fallback provenance: headers on every answer, plus a leading SSE
        # comment when the backup chain served a stream (see routes/chat.py).
        extra_headers, note = begin(call)

        if isinstance(result, StreamRelay):
            chunks = stream_responses_events(
                result.chunks(), call.public_model, restore_map()
            )
            if note:
                chunks = with_fallback_note(chunks, note)
            return streaming(
                chunks,
                result.usage_holder,
                call,
                settings,
                logger,
                extra_headers=extra_headers,
            )
        if is_error_relay(result):
            status = int(result["__status__"])
            call.log(logger, status)
            # Same masking contract as the chat route: an upstream error body
            # must not smuggle the upstream model identity to the client.
            content = result["content"]
            if registry.enabled and isinstance(content, dict):
                content = mask_json_model(
                    content, call.public_model, restore_map()
                )
            return JSONResponse(
                status_code=status, content=content, headers=extra_headers
            )
        if isinstance(result, ServeCompletion):
            result = completion_to_chat_response(call.upstream_model, result)
        if isinstance(result, dict):
            # Exactly one completion record per request, carrying usage.
            if call.stream:
                # Relay through the shared wrapper even though the body is a
                # single synthetic chunk: it owns the "stream ended" record
                # (latency, reason) and the client-stall watchdog.
                chunks = stream_responses_events(
                    _serve_completion_sse_from_chat(result), call.public_model
                )
                if note:
                    chunks = with_fallback_note(chunks, note)
                return streaming(
                    chunks,
                    {"usage": result.get("usage")},
                    call,
                    settings,
                    logger,
                    extra_headers=extra_headers,
                )
            body_out = chat_to_responses_body(result, call.public_model)
            return json_response(
                body_out,
                call,
                logger,
                extra_headers=extra_headers,
                usage=result.get("usage"),
            )

        call.log(logger, 502)
        return openai_error(502, "Unexpected upstream result type.")

    return router


def _serve_completion_sse_from_chat(chat: dict[str, Any]) -> AsyncIterator[bytes]:
    """Wrap an aggregated chat JSON body as a one-chunk SSE byte stream."""

    async def stream() -> AsyncIterator[bytes]:
        choice = chat["choices"][0]
        message = choice["message"]
        delta: dict[str, Any] = {"role": "assistant"}
        for key in ("reasoning_content", "content", "tool_calls"):
            if message.get(key):
                delta[key] = message[key]
        chunk = {
            "id": chat.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": chat.get("created") or int(time.time()),
            "model": chat.get("model") or "unknown",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}
            ],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return stream()
