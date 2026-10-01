"""POST /v1/chat/completions."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import build_auth_dependency
from ..auth.keystore import KeyStore
from ..config import Settings
from ..errors import openai_error
from ..registry import ModelRegistry
from ..sse_mask import mask_json_model, mask_model_field
from ..upstreams.openai_http import StreamRelay
from ..upstreams.opencode_serve import (
    ServeCompletion,
)
from ..upstreams.opencode_serve import (
    completion_to_chat_response as _serve_response,
)
from ._pipeline import (
    dispatch,
    is_error_relay,
    new_call,
    openai_error_factory,
    parse_json_object,
    relay_stream,
)

logger = logging.getLogger("proxy_opencode.chat")


def make_router(
    settings: Settings, keystore: KeyStore, registry: ModelRegistry
) -> APIRouter:
    auth = build_auth_dependency(settings, keystore)
    app_router = APIRouter()

    @app_router.post("/v1/chat/completions")
    async def handler(request: Request, _: str = Depends(auth)) -> Any:
        body = await parse_json_object(request, settings.payload_dump_dir)
        if body is None:
            return openai_error(
                400,
                "Request body must be a valid JSON object.",
                err_type="invalid_request_error",
            )

        call = new_call(
            request, body, settings, registry, log_message="chat completion"
        )
        adapter = request.app.state.adapter
        result = await dispatch(
            adapter,
            call,
            openai_error_factory,
            logger=logger,
            unhandled_message="unhandled chat error",
        )
        if isinstance(result, JSONResponse):
            return result

        model = call.public_model or call.upstream_model

        if isinstance(result, StreamRelay):
            chunks_iter = result.chunks()
            if registry.enabled:
                chunks_iter = mask_model_field(chunks_iter, call.public_model)
            return StreamingResponse(
                relay_stream(
                    chunks_iter,
                    result.usage_holder,
                    lambda status, usage=None: call.log(logger, status, usage),
                    request_id=call.request_id,
                    model=call.upstream_model,
                    client=call.client,
                ),
                status_code=200,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        if is_error_relay(result):  # relayed upstream error, original status
            status = int(result["__status__"])
            call.log(logger, status)
            # The upstream error body must pass the same masking as a success
            # body: a 4xx/5xx payload can still name the upstream model.
            content = result["content"]
            if registry.enabled and isinstance(content, dict):
                content = mask_json_model(content, call.public_model)
            return JSONResponse(status_code=status, content=content)
        if isinstance(result, dict):  # openai passthrough non-stream JSON
            if registry.enabled:
                mask_json_model(result, call.public_model)
            call.log(logger, 200, result.get("usage"))
            return JSONResponse(status_code=200, content=result)
        if isinstance(result, ServeCompletion):
            response = _serve_response(model, result)
            call.log(logger, 200, response.get("usage"))
            if call.stream:
                return _synthetic_stream(model, response)
            return JSONResponse(status_code=200, content=response)

        call.log(logger, 502)
        return openai_error(502, "Unexpected upstream result type.")

    return app_router


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
