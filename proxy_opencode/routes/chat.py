"""POST /v1/chat/completions."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import Settings
from ..errors import openai_error
from ..security import build_auth_dependency
from ..upstreams import build_adapter
from ..upstreams.opencode_serve import (
    ServeAdapter,
    ServeCompletion,
    ServeError,
    WaitTimeoutError,
)
from ..upstreams.fields import filter_payload
from ..upstreams.openai_http import OpenAIHttpAdapter, StreamRelay

logger = logging.getLogger("proxy_opencode.chat")


def _log(request: Request, request_id: str, model: str, **fields: Any) -> None:
    logger.info(
        "chat completion",
        extra={"request_id": request_id, "model": model, **fields},
    )


def make_router(settings: Settings) -> APIRouter:
    auth = build_auth_dependency(settings)
    app_router = APIRouter()

    @app_router.post("/v1/chat/completions")
    async def handler(request: Request, _: str = Depends(auth)) -> Any:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        try:
            body = await request.json()
        except Exception:
            return openai_error(400, "Request body must be valid JSON.")
        if not isinstance(body, dict):
            return openai_error(400, "Request body must be a JSON object.")

        payload = filter_payload(body, settings.reasoning_passthrough)
        model = str(payload.get("model") or "")
        stream = bool(payload.get("stream"))

        def log(status: int, usage: Any = None) -> None:
            _log(
                request,
                request_id,
                model,
                stream=stream,
                has_tools=bool(payload.get("tools")),
                status=status,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                usage=usage,
            )

        adapter = request.app.state.adapter
        try:
            result = await adapter.chat(payload)
        except WaitTimeoutError as exc:
            log(504)
            return openai_error(
                504, str(exc), err_type="timeout_error", code="upstream_timeout"
            )
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

        if isinstance(result, StreamRelay):
            return StreamingResponse(
                _relay(result, log),
                status_code=200,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        if isinstance(result, dict) and "__status__" in result:  # error relay
            log(int(result["__status__"]))
            return JSONResponse(
                status_code=int(result["__status__"]), content=result["content"]
            )
        if isinstance(result, dict):  # openai passthrough non-stream JSON
            log(200, result.get("usage") if isinstance(result, dict) else None)
            return JSONResponse(status_code=200, content=result)
        if isinstance(result, ServeCompletion):
            response = _serve_response(model, result)
            log(200, response.get("usage"))
            if stream:
                return _synthetic_stream(model, response)
            return JSONResponse(status_code=200, content=response)

        log(502)
        return openai_error(502, "Unexpected upstream result type.")

    return app_router


def _serve_response(model: str, result: ServeCompletion) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
    if result.reasoning:
        message["reasoning_content"] = result.reasoning
    if result.tool_calls:
        message["tool_calls"] = result.tool_calls
    metadata: dict[str, Any] = {"adapter": ServeAdapter.name}
    if result.tool_calls:
        metadata["tools_source"] = "json-contract-bridge"
    if result.internal_tool_calls:
        metadata["internal_tools"] = result.internal_tool_calls
        metadata["internal_tools_source"] = "opencode-agent"
    response: dict[str, Any] = {
        "id": f"chatcmpl-serve-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if result.tool_calls else "stop",
            }
        ],
        "usage": result.usage or None,
        "metadata": metadata,
    }
    return response


def _synthetic_stream(model: str, response: dict[str, Any]) -> StreamingResponse:
    choice = response["choices"][0]
    message = choice["message"]
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("reasoning_content"):
        delta["reasoning_content"] = message["reasoning_content"]
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = message["tool_calls"]
    chunk = {
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}
        ],
        "metadata": {**response.get("metadata", {}), "synthetic_stream": True},
    }

    async def synth() -> AsyncIterator[bytes]:
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        synth(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Opencode-Serve-Synthetic-Stream": "true",
        },
    )


async def _relay(relay: StreamRelay, log: Any) -> AsyncIterator[bytes]:
    async for chunk in relay.chunks():
        yield chunk
    log(200, relay.usage_holder.get("usage"))


