"""FastAPI application: compliant OpenAI-compatible reverse proxy gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import opencode_cli
from .config import Settings, load_settings
from .opencode_cli import CliError, ModelsCache
from .ratelimit import RateLimiter

logger = logging.getLogger("proxy_opencode")

# Fields forwarded verbatim when present in chat completion requests.
_PASSTHROUGH_FIELDS = (
    "model",
    "messages",
    "stream",
    "tools",
    "tool_choice",
    "response_format",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "user",
    "logprobs",
    "top_logprobs",
    "parallel_tool_calls",
)
# Reasoning-related fields, only forwarded when REASONING_PASSTHROUGH=true.
_REASONING_FIELDS = (
    "reasoning_effort",
    "thinking",
    "include_reasoning",
    "reasoning",
)


def _openai_error(
    status: int, message: str, err_type: str = "api_error", code: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": err_type,
                "param": None,
                "code": code,
            }
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="proxy_opencode", version="0.1.0")
    app.state.settings = settings
    app.state.ratelimiter = RateLimiter(settings.requests_per_minute)
    app.state.cli_models_cache = opencode_cli.ModelsCache(settings)
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        timeout=httpx.Timeout(300.0, connect=30.0),
        headers={
            "Authorization": f"Bearer {settings.upstream_api_key}",
            "Content-Type": "application/json",
        },
    )
    if settings.is_opencode_cli:
        app.state.cli_models_cache = opencode_cli.ModelsCache(settings)

    async def require_gateway_key(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if settings.dev_open:
            # Development mode: no keys configured, accept anything.
            return token or "dev-open"
        if not token or token not in settings.gateway_api_keys:
            raise _auth_error()
        app = request.app
        if not app.state.ratelimiter.allow(token):
            raise _rate_limit_error()
        return token

    class _GatewayAuthError(Exception):
        def __init__(self, response: JSONResponse) -> None:
            self.response = response

    def _auth_error() -> Exception:
        return _GatewayAuthError(
            _openai_error(
                401,
                "Invalid or missing gateway API key.",
                err_type="authentication_error",
                code="invalid_api_key",
            )
        )

    def _rate_limit_error() -> Exception:
        return _GatewayAuthError(
            _openai_error(
                429,
                "Rate limit exceeded for this gateway API key.",
                err_type="rate_limit_error",
                code="rate_limit_exceeded",
            )
        )

    @app.exception_handler(_GatewayAuthError)
    async def _handle_gateway_error(
        request: Request, exc: _GatewayAuthError
    ) -> JSONResponse:
        return exc.response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstream": settings.upstream_base_url,
            "dev_open": settings.dev_open,
        }

    @app.get("/v1/models")
    async def list_models(_: str = Depends(require_gateway_key)) -> Any:
        if settings.is_opencode_cli:
            cache: ModelsCache = app.state.cli_models_cache
            try:
                models = await asyncio.to_thread(cache.get)
            except CliError as exc:
                return _openai_error(502, f"opencode CLI upstream failed: {exc}")
            except Exception as exc:
                return _openai_error(502, f"opencode CLI upstream failed: {exc}")
            return {
                "object": "list",
                "data": [
                    {
                        "id": m,
                        "object": "model",
                        "created": 0,
                        "owned_by": "opencode-cli",
                    }
                    for m in models
                ],
                "metadata": {"adapter": "opencode-cli"},
            }
        client: httpx.AsyncClient = app.state.http_client
        try:
            resp = await client.get("/v1/models")
        except httpx.HTTPError as exc:
            return _openai_error(502, f"Upstream request failed: {exc}")
        return _relay_json(resp)

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request, _: str = Depends(require_gateway_key)
    ) -> Any:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()

        try:
            body = await request.json()
        except Exception:
            return _openai_error(400, "Request body must be valid JSON.")

        payload: dict[str, Any] = {
            k: v for k, v in body.items() if k in _PASSTHROUGH_FIELDS
        }
        if not payload.get("messages"):
            return _openai_error(
                400, "'messages' is required.", err_type="invalid_request_error"
            )

        if settings.is_opencode_cli:
            return await _cli_chat(body, request_id, started)

        if settings.reasoning_passthrough:
            for k in _REASONING_FIELDS:
                if k in body:
                    payload[k] = body[k]

        model = payload.get("model")
        stream = bool(payload.get("stream"))
        has_tools = bool(payload.get("tools"))

        client: httpx.AsyncClient = app.state.http_client

        def _log(status: int, usage: dict[str, Any] | None = None) -> None:
            logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "model": model,
                    "stream": stream,
                    "has_tools": has_tools,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "usage": usage,
                },
            )

        if stream:
            return await _stream_chat(client, payload, _log)

        try:
            resp = await client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            _log(502)
            return _openai_error(502, f"Upstream request failed: {exc}")

        usage = None
        try:
            usage = resp.json().get("usage")
        except Exception:
            pass
        _log(resp.status_code, usage)
        return _relay_json(resp)

    async def _cli_chat(
        body: dict[str, Any], request_id: str, started: float
    ) -> Any:
        """opencode-cli upstream: local, official CLI, restricted."""
        model = body.get("model") or ""
        stream = bool(body.get("stream"))

        def _log(status: int, exit_code: int | None = None) -> None:
            logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "adapter": "opencode-cli",
                    "model": model,
                    "stream": stream,
                    "status": status,
                    "exit_code": exit_code,
                    "latency_ms": round(
                        (time.perf_counter() - started) * 1000, 1
                    ),
                },
            )

        unsupported = [
            k
            for k in ("tools", "tool_calls", "response_format", "reasoning_effort")
            if body.get(k)
        ]
        if unsupported:
            _log(400)
            return _openai_error(
                400,
                "opencode-cli adapter does not support tool/reasoning "
                f"passthrough; unsupported fields: {', '.join(unsupported)}.",
                err_type="invalid_request_error",
            )
        if not model or not opencode_cli.model_allowed(settings, model):
            _log(400)
            return _openai_error(
                400,
                f"model {model!r} is not allowed by "
                "OPENCODE_ALLOWED_MODEL_PREFIXES.",
                err_type="invalid_request_error",
                code="model_not_allowed",
            )
        try:
            prompt = opencode_cli.build_prompt(body["messages"])
        except CliError as exc:
            _log(400)
            return _openai_error(400, str(exc), err_type="invalid_request_error")

        try:
            result = await asyncio.to_thread(
                opencode_cli.run_completion, settings, model, prompt
            )
        except CliError as exc:
            _log(502)
            return _openai_error(502, f"opencode CLI upstream failed: {exc}")
        _log(200, result.exit_code)

        created = int(time.time())
        if stream:
            chunk = {
                "id": f"chatcmpl-cli-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": result.stdout},
                        "finish_reason": "stop",
                    }
                ],
                "metadata": {
                    "adapter": "opencode-cli",
                    "synthetic_stream": True,
                },
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
                    "X-Opencode-CLI-Synthetic-Stream": "true",
                },
            )

        return JSONResponse(
            status_code=200,
            content={
                "id": f"chatcmpl-cli-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": result.stdout,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "metadata": {"adapter": "opencode-cli"},
            },
        )

    async def _stream_chat(
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        log: Any,
    ) -> StreamingResponse | JSONResponse:
        req = client.build_request("POST", "/v1/chat/completions", json=payload)
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            log(502)
            return _openai_error(502, f"Upstream request failed: {exc}")

        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            log(resp.status_code)
            return _relay_raw(resp, body)

        usage_holder: dict[str, Any] = {}

        async def relay() -> AsyncIterator[bytes]:
            status = 200
            try:
                async for chunk in resp.aiter_bytes():
                    # Best-effort capture of usage from streamed chunks for logging.
                    if b"data:" in chunk and b"usage" in chunk:
                        for line in chunk.split(b"\n"):
                            line = line.strip()
                            if line.startswith(b"data:") and b"usage" in line:
                                try:
                                    data = json.loads(line[5:])
                                    if isinstance(data, dict) and data.get("usage"):
                                        usage_holder["usage"] = data["usage"]
                                except Exception:
                                    pass
                    yield chunk
            finally:
                await resp.aclose()
                log(status, usage_holder.get("usage"))

        return StreamingResponse(
            relay(),
            status_code=200,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _relay_json(resp: httpx.Response) -> JSONResponse:
        try:
            content = resp.json()
        except Exception:
            content = {
                "error": {
                    "message": f"Upstream returned non-JSON ({resp.status_code}).",
                    "type": "api_error",
                    "param": None,
                    "code": None,
                }
            }
        return JSONResponse(status_code=resp.status_code, content=content)

    def _relay_raw(resp: httpx.Response, body: bytes) -> JSONResponse:
        try:
            content = json.loads(body)
        except Exception:
            content = {
                "error": {
                    "message": f"Upstream error ({resp.status_code}).",
                    "type": "api_error",
                    "param": None,
                    "code": None,
                }
            }
        return JSONResponse(status_code=resp.status_code, content=content)

    return app


app = create_app()
