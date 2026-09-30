"""POST /v1/messages — Anthropic Messages API (Claude Code compatible).

Auth accepts both `x-api-key` and `Authorization: Bearer` (Claude Code uses
either depending on ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN). Errors use
the Anthropic `{"type":"error","error":{...}}` envelope on this route.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import build_auth_dependency
from ..auth.keystore import KeyStore
from ..config import Settings
from ..formats.anthropic_proto import (
    chat_to_anthropic_body,
    stream_anthropic_events,
    to_chat_payload,
)
from ..registry import ModelRegistry
from ..upstreams.openai_http import StreamRelay
from ..upstreams.opencode_serve import ServeCompletion, completion_to_chat_response
from ._pipeline import (
    dispatch,
    is_error_relay,
    new_call,
    parse_json_object,
)

logger = logging.getLogger("proxy_opencode.anthropic")

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def anthropic_error(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


def _error_factory(
    status: int, message: str, err_type: str, code: str | None
) -> JSONResponse:
    """ErrorFactory adapter: Anthropic envelope, `code` is not part of it."""
    return anthropic_error(status, err_type, message)


def _chat_body_sse(chat: dict[str, Any]) -> AsyncIterator[bytes]:
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


def _upstream_error_message(content: Any) -> str:
    if isinstance(content, dict):
        err = content.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "")
    return ""


def make_router(
    settings: Settings, keystore: KeyStore, registry: ModelRegistry
) -> APIRouter:
    auth = build_auth_dependency(settings, keystore, error_style="anthropic")
    router = APIRouter()

    @router.post("/v1/messages")
    async def handler(request: Request, _: str = Depends(auth)) -> Any:
        body = await parse_json_object(request)
        if body is None:
            return anthropic_error(
                400, "invalid_request_error", "Request body must be a valid JSON object."
            )

        try:
            payload = to_chat_payload(body)
        except Exception:
            return anthropic_error(
                400, "invalid_request_error", "Malformed Messages request."
            )

        call = new_call(
            request,
            payload,
            settings,
            registry,
            log_message="anthropic messages completion",
        )
        adapter = request.app.state.adapter
        result = await dispatch(
            adapter,
            call,
            _error_factory,
            logger=logger,
            unhandled_message="unhandled anthropic error",
        )
        if isinstance(result, JSONResponse):
            return result

        if isinstance(result, StreamRelay):
            call.log(logger, 200)
            return StreamingResponse(
                stream_anthropic_events(result.chunks(), call.public_model),
                status_code=200,
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        if is_error_relay(result):
            status = int(result["__status__"])
            call.log(logger, status)
            # Relay upstream errors in the Anthropic envelope.
            message = _upstream_error_message(result["content"])
            return anthropic_error(status, "api_error", message or "Upstream error.")
        if isinstance(result, ServeCompletion):
            result = completion_to_chat_response(call.upstream_model, result)
        if isinstance(result, dict):
            # Exactly one completion record per request, carrying usage.
            if call.stream:
                call.log(logger, 200, result.get("usage"))
                return StreamingResponse(
                    stream_anthropic_events(_chat_body_sse(result), call.public_model),
                    status_code=200,
                    media_type="text/event-stream",
                    headers=_SSE_HEADERS,
                )
            body_out = chat_to_anthropic_body(result, call.public_model)
            call.log(logger, 200, result.get("usage"))
            return JSONResponse(status_code=200, content=body_out)

        call.log(logger, 502)
        return anthropic_error(502, "api_error", "Unexpected upstream result type.")

    return router
