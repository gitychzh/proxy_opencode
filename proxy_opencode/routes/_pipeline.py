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

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..errors import openai_error
from ..registry import ModelRegistry
from ..upstreams.fields import filter_payload
from ..upstreams.opencode_serve import ServeError, WaitTimeoutError

# (status, message, err_type, code) -> protocol-shaped error response.
ErrorFactory = Callable[[int, str, str, "str | None"], JSONResponse]


def openai_error_factory(
    status: int, message: str, err_type: str, code: str | None
) -> JSONResponse:
    """ErrorFactory speaking the OpenAI error envelope."""
    return openai_error(status, message, err_type=err_type, code=code)


async def parse_json_object(request: Request) -> dict[str, Any] | None:
    """Parse the request body as a JSON object; None when it is not one."""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


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
