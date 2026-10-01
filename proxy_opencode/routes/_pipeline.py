"""Shared request pipeline for the three protocol routes.

`/v1/chat/completions`, `/v1/responses` and `/v1/messages` are three thin
protocol shims over one pipeline: parse JSON -> convert to a chat payload ->
resolve the public model alias -> whitelist-filter -> call the adapter ->
shape the protocol response. Only the *convert* and *shape* steps differ, so
everything in between lives here.

Keeping the middle in one place is deliberate: the triplicated copy used to
drift (e.g. the serve-mode branch logged the same completion twice) and the
error mapping was maintained three times.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..errors import openai_error
from ..registry import ModelRegistry
from ..upstreams.fields import filter_payload
from ..upstreams.opencode_serve import ServeError, WaitTimeoutError

# (status, message, err_type, code) -> protocol-shaped error response.
ErrorFactory = Callable[[int, str, str, "str | None"], JSONResponse]

logger = logging.getLogger("proxy_opencode.pipeline")


def openai_error_factory(
    status: int, message: str, err_type: str, code: str | None
) -> JSONResponse:
    """ErrorFactory speaking the OpenAI error envelope."""
    return openai_error(status, message, err_type=err_type, code=code)


async def parse_json_object(
    request: Request, dump_dir: str = ""
) -> dict[str, Any] | None:
    """Parse the request body as a JSON object; None when it is not one.

    When `dump_dir` is set (PAYLOAD_DUMP_DIR), the raw body is also persisted
    for offline prompt-bloat auditing. The dump happens BEFORE whitelist
    filtering so client-side waste (tool schemas, system prompts) is visible.
    """
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    if dump_dir:
        _dump_payload(dump_dir, request.url.path, body)
    return body


_DUMP_KEEP = 200


def _protocol_slug(path: str) -> str:
    if path.endswith("/chat/completions"):
        return "chat"
    if path.endswith("/responses"):
        return "responses"
    if path.endswith("/messages"):
        return "messages"
    return "other"


def _dump_payload(directory: str, path: str, body: dict[str, Any]) -> None:
    """Persist one raw client body for offline bloat auditing (best-effort).

    NOTE: this writes full request bodies (prompts included) to disk. It is
    off unless PAYLOAD_DUMP_DIR is set, and it deliberately bypasses the
    logging red line (nothing is written to the logs) — treat the dump
    directory as sensitive and keep it off production buckets.
    """
    try:
        dump_dir = Path(directory)
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        name = f"{stamp}_{uuid.uuid4().hex[:8]}_{_protocol_slug(path)}.json"
        (dump_dir / name).write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Prune oldest-first by mtime: the filename only carries HHMMSS, so
        # sorting names would delete the wrong files as soon as the dump
        # directory spans more than one day.
        files = sorted(dump_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - _DUMP_KEEP)]:
            old.unlink(missing_ok=True)
    except OSError:
        logger.warning("payload dump failed", exc_info=True)


@dataclass
class ChatCall:
    """One normalised chat request flowing through the shared pipeline."""

    payload: dict[str, Any]
    public_model: str
    stream: bool
    request_id: str
    client: str
    started: float
    log_message: str = "chat completion"
    extra_log_fields: dict[str, Any] = field(default_factory=dict)

    @property
    def upstream_model(self) -> str:
        return str(self.payload.get("model") or "")

    def log(self, logger: logging.Logger, status: int, usage: Any = None) -> None:
        logger.info(
            self.log_message,
            extra={
                "request_id": self.request_id,
                "model": self.upstream_model,
                "client": self.client,
                "stream": self.stream,
                "has_tools": bool(self.payload.get("tools")),
                "status": status,
                "latency_ms": round((time.perf_counter() - self.started) * 1000, 1),
                "usage": usage,
                **self.extra_log_fields,
            },
        )


def new_call(
    request: Request,
    payload: dict[str, Any],
    settings: Settings,
    registry: ModelRegistry,
    *,
    log_message: str = "chat completion",
) -> ChatCall:
    """Resolve the model alias, whitelist-filter and build the call context."""
    requested = str(payload.get("model") or "")
    payload["model"] = registry.resolve(requested)
    public_model = registry.public_id(requested)
    payload = filter_payload(payload, settings.reasoning_passthrough)
    return ChatCall(
        payload=payload,
        public_model=public_model,
        stream=bool(payload.get("stream")),
        request_id=uuid.uuid4().hex[:12],
        client=request.client.host if request.client else "-",
        started=time.perf_counter(),
        log_message=log_message,
    )


async def dispatch(
    adapter: Any,
    call: ChatCall,
    on_error: ErrorFactory,
    *,
    logger: logging.Logger,
    unhandled_message: str,
) -> Any:
    """Call the adapter, mapping every failure onto the protocol's envelope.

    Returns the adapter result on success, otherwise an error response (which
    the caller returns verbatim). The catch-all keeps the response contract
    while leaving a full traceback in the log — message content is never
    logged.
    """
    try:
        return await adapter.chat(call.payload)
    except WaitTimeoutError as exc:
        call.log(logger, 504)
        return on_error(504, str(exc), "timeout_error", "upstream_timeout")
    except ServeError as exc:
        call.log(logger, 502)
        return on_error(502, str(exc), "api_error", None)
    except ConnectionError as exc:
        call.log(logger, 502)
        return on_error(502, str(exc), "api_connection_error", "upstream_unreachable")
    except ValueError as exc:
        call.log(logger, 400)
        return on_error(400, str(exc), "invalid_request_error", None)
    except Exception:
        call.log(logger, 500)
        logger.exception(
            unhandled_message,
            extra={
                "request_id": call.request_id,
                "model": call.upstream_model,
                "client": call.client,
            },
        )
        return on_error(500, "Internal gateway error.", "api_error", None)


def is_error_relay(result: Any) -> bool:
    """True when an adapter returned a relayed upstream error body."""
    return isinstance(result, dict) and "__status__" in result


async def relay_stream(
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
    would otherwise vanish silently from the logs. All three protocol
    routes wrap their streaming responses in this so the completion record
    carries real latency, chunk count and (when captured) usage.

    On client disconnect the inner generator is closed explicitly so the
    upstream response is released deterministically instead of waiting for
    GC.
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
        # Release the upstream deterministically. `chunks` is annotated as an
        # AsyncIterator but every caller passes an async generator, which
        # carries `aclose()`.
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
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
