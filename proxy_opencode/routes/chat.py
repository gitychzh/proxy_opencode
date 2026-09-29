"""POST /v1/chat/completions."""

from __future__ import annotations

import asyncio
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
from ..registry import ModelRegistry
from ..sse_mask import mask_model_field
from ..upstreams.fields import filter_payload
from ..upstreams.openai_http import StreamRelay
from ..upstreams.opencode_serve import (
    ServeCompletion,
    ServeError,
    WaitTimeoutError,
)
from ..upstreams.opencode_serve import (
    completion_to_chat_response as _serve_response,
)

logger = logging.getLogger("proxy_opencode.chat")


def _log(
    request: Request, request_id: str, model: str, client: str, **fields: Any
) -> None:
    logger.info(
        "chat completion",
        extra={"request_id": request_id, "model": model, "client": client, **fields},
    )


def make_router(
    settings: Settings, keystore: KeyStore, registry: ModelRegistry
) -> APIRouter:
    auth = build_auth_dependency(settings, keystore)
    app_router = APIRouter()

    @app_router.post("/v1/chat/completions")
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

        payload = filter_payload(body, settings.reasoning_passthrough)
        requested_model = str(payload.get("model") or "")
        # Alias layer: any requested model resolves to the upstream model;
        # the public id is what users see in every response below.
        payload["model"] = registry.resolve(requested_model)
        public_model = registry.public_id(requested_model)
        model = str(payload["model"])
        stream = bool(payload.get("stream"))

        def log(status: int, usage: Any = None) -> None:
            _log(
                request,
                request_id,
                model,
                client,
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
        except Exception:
            # Catch-all: keep the response contract and leave a full traceback
            # in the log (message content is never logged).
            log(500)
            logger.exception(
                "unhandled chat error",
                extra={
                    "request_id": request_id,
                    "model": model,
                    "client": request.client.host if request.client else "-",
                },
            )
            return openai_error(500, "Internal gateway error.")

        if isinstance(result, StreamRelay):
            chunks_iter = result.chunks()
            if registry.enabled:
                chunks_iter = mask_model_field(chunks_iter, public_model)
            return StreamingResponse(
                _relay(
                    chunks_iter,
                    result.usage_holder,
                    log,
                    request_id=request_id,
                    model=model,
                    client=client,
                ),
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
            if registry.enabled:
                result["model"] = public_model
            log(200, result.get("usage") if isinstance(result, dict) else None)
            return JSONResponse(status_code=200, content=result)
        if isinstance(result, ServeCompletion):
            response = _serve_response(public_model or model, result)
            log(200, response.get("usage"))
            if stream:
                return _synthetic_stream(public_model or model, response)
            return JSONResponse(status_code=200, content=response)

        log(502)
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


async def _relay(
    chunks: AsyncIterator[bytes],
    usage_holder: dict[str, Any],
    log: Any,
    *,
    request_id: str,
    model: str,
    client: str,
) -> AsyncIterator[bytes]:
    """Relay upstream SSE bytes to the client with stream lifecycle logging.

    Every stream terminates with a "stream ended" record — including the
    abnormal ends (client disconnect, upstream mid-stream failure) that
    would otherwise vanish silently from the logs.
    """
    started = time.perf_counter()
    chunks_count = 0
    ttfb_ms: float | None = None
    reason = "completed"
    try:
        async for chunk in chunks:
            if ttfb_ms is None:
                ttfb_ms = round((time.perf_counter() - started) * 1000, 1)
            chunks_count += 1
            yield chunk
        log(200, usage_holder.get("usage"))
    except (asyncio.CancelledError, GeneratorExit):
        reason = "client_disconnected"
        raise
    except Exception as exc:
        reason = f"relay_error:{type(exc).__name__}"
        logger.exception(
            "stream relay failed",
            extra={
                "request_id": request_id,
                "model": model,
                "client": client,
                "chunks": chunks_count,
                "reason": reason,
                "error_detail": str(exc)[:200],
            },
        )
        raise
    finally:
        logger.info(
            "stream ended",
            extra={
                "request_id": request_id,
                "model": model,
                "client": client,
                "chunks": chunks_count,
                "ttfb_ms": ttfb_ms,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "reason": reason,
            },
        )


