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
from fastapi.responses import JSONResponse, StreamingResponse

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
from ..upstreams.fields import filter_payload
from ..upstreams.openai_http import StreamRelay
from ..upstreams.opencode_serve import (
    ServeCompletion,
    ServeError,
    WaitTimeoutError,
    completion_to_chat_response,
)

logger = logging.getLogger("proxy_opencode.responses")


def make_router(
    settings: Settings, keystore: KeyStore, registry: ModelRegistry
) -> APIRouter:
    auth = build_auth_dependency(settings, keystore)
    router = APIRouter()

    @router.post("/v1/responses")
    async def handler(request: Request, _: str = Depends(auth)) -> Any:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        client = request.client.host if request.client else "-"
        try:
            body = await request.json()
        except Exception:
            return openai_error(400, "Request body must be valid JSON.")
        if not isinstance(body, dict):
            return openai_error(400, "Request body must be a JSON object.")

        try:
            payload = to_chat_payload(body)
        except Exception:
            return openai_error(
                400, "Malformed Responses request.", err_type="invalid_request_error"
            )
        requested_model = str(payload.get("model") or "")
        payload["model"] = registry.resolve(requested_model)
        public_model = registry.public_id(requested_model)
        payload = filter_payload(payload, settings.reasoning_passthrough)
        # filter_payload drops unknown keys; stream flag is preserved.
        stream = bool(payload.get("stream"))

        def log(status: int, usage: Any = None) -> None:
            logger.info(
                "responses completion",
                extra={
                    "request_id": request_id,
                    "model": str(payload["model"]),
                    "client": client,
                    "stream": stream,
                    "has_tools": bool(payload.get("tools")),
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "usage": usage,
                },
            )

        adapter = request.app.state.adapter
        try:
            result = await adapter.chat(payload)
        except WaitTimeoutError as exc:
            log(504)
            return openai_error(504, str(exc), err_type="timeout_error", code="upstream_timeout")
        except ServeError as exc:
            log(502)
            return openai_error(502, str(exc))
        except ConnectionError as exc:
            log(502)
            return openai_error(
                502, str(exc), err_type="api_connection_error", code="upstream_unreachable"
            )
        except ValueError as exc:
            log(400)
            return openai_error(400, str(exc), err_type="invalid_request_error")
        except Exception:
            log(500)
            logger.exception(
                "unhandled responses error", extra={"request_id": request_id}
            )
            return openai_error(500, "Internal gateway error.")

        if isinstance(result, StreamRelay):
            chunks = result.chunks()
            log(200)
            return StreamingResponse(
                stream_responses_events(chunks, public_model),
                status_code=200,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        if isinstance(result, dict) and "__status__" in result:
            log(int(result["__status__"]))
            return JSONResponse(
                status_code=int(result["__status__"]), content=result["content"]
            )
        if isinstance(result, ServeCompletion):
            result = completion_to_chat_response(str(payload["model"]), result)
            log(200, result.get("usage"))
        if isinstance(result, dict):
            if stream:
                return StreamingResponse(
                    stream_responses_events(
                        _serve_completion_sse_from_chat(result), public_model
                    ),
                    status_code=200,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            body_out = chat_to_responses_body(result, public_model)
            log(200)
            return JSONResponse(status_code=200, content=body_out)

        log(502)
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
