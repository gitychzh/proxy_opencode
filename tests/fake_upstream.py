"""Reusable fake OpenAI-compatible upstream for tests and local smoke runs.

Endpoints:
- GET  /v1/models
- POST /v1/chat/completions  (stream and non-stream)

Every chat response echoes request metadata (auth key presence/sha256 prefix,
wire model, stream flag, tools, reasoning fields) under ``metadata.echo`` so
tests can assert passthrough behaviour. Sensitive values are never logged or
returned verbatim: only a boolean and a short SHA-256 prefix of the key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DEFAULT_MODEL = "fake-openai-model"

_REASONING_FIELDS = ("reasoning_effort", "thinking", "include_reasoning", "reasoning")


def _key_fingerprint(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    return {
        "authorization_present": bool(token),
        "authorization_sha256_12": hashlib.sha256(token.encode()).hexdigest()[:12]
        if token
        else None,
    }


def create_app(default_model: str = DEFAULT_MODEL) -> FastAPI:
    app = FastAPI(title="fake-upstream")

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": default_model,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "fake-upstream",
                }
            ],
            "metadata": {"echo": _key_fingerprint(request)},
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Request body must be valid JSON.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": None,
                    }
                },
            )

        model = body.get("model") or default_model
        stream = bool(body.get("stream"))
        echo = {
            **_key_fingerprint(request),
            "model": model,
            "stream": stream,
            "tools": body.get("tools"),
            "tool_choice": body.get("tool_choice"),
            "reasoning": {
                k: body[k] for k in _REASONING_FIELDS if k in body
            },
        }

        if stream:
            return StreamingResponse(
                _sse_chunks(model, echo),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        return JSONResponse(_completion_payload(model, echo))

    return app


def _completion_payload(model: str, echo: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-fake-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fake upstream reply"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        "metadata": {"echo": echo},
    }


async def _sse_chunks(model: str, echo: dict[str, Any]) -> AsyncIterator[bytes]:
    base = {
        "id": f"chatcmpl-fake-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    first = {**base, "choices": [{"index": 0, "delta": {"content": "fake "}, "finish_reason": None}]}
    yield f"data: {json.dumps(first)}\n\n".encode()
    second = {
        **base,
        "choices": [{"index": 0, "delta": {"content": "reply"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(second)}\n\n".encode()
    final = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        "metadata": {"echo": echo},
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fake upstream server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
