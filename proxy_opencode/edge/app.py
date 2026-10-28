"""The edge ASGI app: forward, decide, relay. No buffering on the happy path.

Deliberate choices:

* **Raw ASGI instead of FastAPI routes.** A pass-through must not inherit
  request validation, CORS defaults or response models, and SSE must be forwarded
  chunk-by-chunk the moment it arrives (``aiter_raw`` + ``more_body``), so the
  edge holds no frames.
* **Buffering only where it is needed to decide.** Success bodies are streamed;
  only error bodies (to classify a quota verdict) and fallback JSON bodies (to
  mask the model id) are read whole, and those are small.
* **The switch happens before the first byte.** Once ``http.response.start`` is
  sent the primary is committed to: a truncated stream can never be repaired.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from .. import __version__
from .config import EdgeSettings, load_edge_settings
from .policy import (
    failover_reason,
    filter_request_headers,
    filter_response_headers,
    header_value,
    is_fallback_path,
    mask_model_in_json,
    rewrite_for_fallback,
)

logger = logging.getLogger("proxy_opencode.edge")

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

_MAX_ERROR_BODY = 1 << 20  # error bodies are metadata; 1 MiB is already absurd


class _BodyTooLarge(Exception):
    pass


class _ClientGone(Exception):
    pass


class EdgeApp:
    """ASGI callable: ``primary`` transparently, ``fallback`` when it cannot serve."""

    def __init__(
        self,
        settings: EdgeSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or load_edge_settings()
        self._client = client
        self._probe_task: asyncio.Task[None] | None = None
        self._mode = self._settings.mode
        self._primary_alive = True
        self._primary_error = ""
        self._primary_down_since = 0.0
        self._started_at = time.monotonic()
        self._stats: dict[str, int] = {
            "requests": 0,
            "primary": 0,
            "fallback": 0,
            "fallback_failed": 0,
        }

    # ---------------------------------------------------------------- ASGI

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self._lifespan(receive, send)
            return
        if kind != "http":
            return
        path = scope.get("path") or "/"
        if path in ("/healthz", "/health"):
            await self._health(send)
            return
        if path.startswith("/edge/"):
            await self._admin(scope, receive, send)
            return
        await self._forward(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._probe_task = asyncio.create_task(self._probe_loop())
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self._shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _shutdown(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):
                pass
            self._probe_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- probing

    async def _probe_loop(self) -> None:
        while True:
            await self._probe_once()
            await asyncio.sleep(self._settings.probe_interval_s)

    async def _probe_once(self) -> None:
        url = f"{self._settings.primary_base_url}/healthz"
        try:
            resp = await self._http().get(url, timeout=self._settings.probe_timeout_s)
            ok = resp.status_code < 500
            error = "" if ok else f"healthz {resp.status_code}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means "not serving"
            ok, error = False, f"{type(exc).__name__}: {exc}"[:200]
        if ok and not self._primary_alive:
            logger.warning(
                "edge: primary recovered, routing back to primary base=%s",
                self._settings.primary_base_url,
                extra={"route": "primary"},
            )
        elif not ok and self._primary_alive:
            logger.warning(
                "edge: primary unavailable (%s), routing to fallback until it answers",
                error,
                extra={"route": "fallback"},
            )
            self._primary_down_since = time.monotonic()
        self._primary_alive = ok
        self._primary_error = error

    # ------------------------------------------------------------ forwarding

    async def _forward(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        settings = self._settings
        method = scope.get("method") or "GET"
        path = scope.get("path") or "/"
        query = (scope.get("query_string") or b"").decode("latin-1")
        target = path + (f"?{query}" if query else "")

        try:
            body = await self._read_body(receive)
        except _BodyTooLarge:
            await self._json(
                send,
                413,
                {"error": {"message": "request body too large", "type": "invalid_request_error"}},
            )
            return
        except _ClientGone:
            return

        headers = filter_request_headers(scope.get("headers") or [])
        self._stats["requests"] += 1
        can_fallback = bool(settings.fallback_base_url) and is_fallback_path(
            path, settings.fallback_paths
        )
        allow_fallback = can_fallback and self._mode in ("auto", "fallback")
        forced_fallback = self._mode == "fallback" and can_fallback
        try_primary = not forced_fallback

        primary_error = ""
        if try_primary:
            resp, primary_error = await self._send(
                f"{settings.primary_base_url}{target}", method, headers, body
            )
            if resp is not None:
                buffered = await self._read_error_body(resp)
                reason = failover_reason(
                    resp.status_code,
                    resp.headers,
                    buffered or b"",
                    failover_statuses=settings.failover_statuses,
                    failover_on_quota=settings.failover_on_quota,
                )
                if reason is None or not allow_fallback:
                    await self._deliver(resp, send, "primary", "", buffered, "")
                    return
                await self._aclose(resp)
                logger.warning(
                    "edge: primary cannot serve (%s), switching to fallback path=%s",
                    reason,
                    path,
                    extra={"route": "fallback", "reason": reason},
                )
                primary_error = reason
            elif not allow_fallback:
                await self._json(
                    send,
                    502,
                    {
                        "error": {
                            "message": (
                                "gateway unreachable and no fallback configured: "
                                f"{primary_error}"
                            ),
                            "type": "api_error",
                            "code": "edge_upstream_unreachable",
                        }
                    },
                )
                return

        await self._serve_fallback(
            send, method, target, headers, body, can_fallback, primary_error
        )

    async def _serve_fallback(
        self,
        send: Send,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes,
        can_fallback: bool,
        primary_error: str,
    ) -> None:
        settings = self._settings
        rewritten = rewrite_for_fallback(body, settings) if can_fallback else None
        if rewritten is None:
            self._stats["fallback_failed"] += 1
            await self._json(
                send,
                502,
                {
                    "error": {
                        "message": (
                            "gateway unavailable and the request cannot be replayed "
                            f"on the backup relay ({primary_error or 'no body'})"
                        ),
                        "type": "api_error",
                        "code": "edge_fallback_unavailable",
                    }
                },
            )
            return

        fb_body, requested_model = rewritten
        fb_headers = [(k, v) for k, v in headers if k != "authorization"]
        if settings.fallback_api_key:
            fb_headers.append(("authorization", f"Bearer {settings.fallback_api_key}"))
        resp, error = await self._send(
            f"{settings.fallback_base_url}{target}", method, fb_headers, fb_body
        )
        if resp is None:
            self._stats["fallback_failed"] += 1
            logger.error(
                "edge: backup relay unreachable (%s); request failed", error,
                extra={"route": "fallback", "reason": error},
            )
            await self._json(
                send,
                502,
                {
                    "error": {
                        "message": f"both the gateway and the backup relay failed: {error}",
                        "type": "api_error",
                        "code": "edge_upstream_unreachable",
                    }
                },
            )
            return

        await self._deliver(resp, send, "fallback", primary_error, None, requested_model)

    async def _send(
        self,
        url: str,
        method: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> tuple[httpx.Response | None, str]:
        client = self._http()
        try:
            request = client.build_request(
                method, url, headers=headers, content=body if body else None
            )
            return await client.send(request, stream=True), ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection refused, DNS, timeout
            return None, f"{type(exc).__name__}: {exc}"[:200]

    # --------------------------------------------------------------- relay

    async def _deliver(
        self,
        resp: httpx.Response,
        send: Send,
        route: str,
        reason: str,
        buffered: bytes | None,
        model: str,
    ) -> None:
        """Relay ``resp`` to the client, streaming unless a rewrite is needed."""

        try:
            self._stats[route] += 1
            status = resp.status_code
            content_type = header_value(resp.headers, "content-type").lower()
            head = filter_response_headers(resp.headers)
            head.append(("x-edge-route", route))
            if reason:
                head.append(("x-edge-fallback-reason", reason[:120]))

            needs_buffer = (
                buffered is not None
                or status >= 400
                or (route == "fallback" and "application/json" in content_type)
            )
            if not needs_buffer:
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(k.encode(), v.encode()) for k, v in head],
                    }
                )
                async for chunk in resp.aiter_raw():
                    if chunk:
                        await send(
                            {"type": "http.response.body", "body": chunk, "more_body": True}
                        )
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return

            payload = buffered if buffered is not None else await resp.aread()
            if route == "fallback" and status < 400 and model:
                payload = mask_model_in_json(payload, model)
            head.append(("content-length", str(len(payload))))
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(k.encode(), v.encode()) for k, v in head],
                }
            )
            await send({"type": "http.response.body", "body": payload, "more_body": False})
        finally:
            await self._aclose(resp)

    async def _read_error_body(self, resp: httpx.Response) -> bytes | None:
        """Only errors are worth buffering: they carry the failover verdict."""

        if resp.status_code < 400:
            return None
        try:
            return await resp.aread()
        except Exception:  # noqa: BLE001 - unreadable error body, treat as empty
            return b""

    # --------------------------------------------------------------- admin

    async def _health(self, send: Send) -> None:
        await self._json(send, 200, self._state())

    def _state(self) -> dict[str, Any]:
        settings = self._settings
        return {
            "status": "ok",
            "role": "edge",
            "version": __version__,
            "mode": self._mode,
            "route": self._preferred_route(),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "primary": {
                "base": settings.primary_base_url,
                "alive": self._primary_alive,
                "last_error": self._primary_error,
                "down_s": (
                    round(time.monotonic() - self._primary_down_since, 1)
                    if not self._primary_alive and self._primary_down_since
                    else 0.0
                ),
            },
            "fallback": {
                "base": settings.fallback_base_url,
                "configured": bool(settings.fallback_base_url),
                "model": settings.fallback_model,
                "paths": sorted(settings.fallback_paths),
                "on_quota": settings.failover_on_quota,
            },
            "stats": dict(self._stats),
        }

    def _preferred_route(self) -> str:
        if self._mode == "fallback":
            return "fallback"
        if self._mode == "primary":
            return "primary"
        if not self._primary_alive and self._settings.fallback_base_url:
            return "fallback"
        return "primary"

    async def _admin(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if not self._admin_ok(scope.get("headers") or []):
            await self._json(
                send,
                404,
                {"error": {"message": "not found", "type": "invalid_request_error"}},
            )
            return
        path = scope.get("path") or "/"
        method = scope.get("method") or "GET"
        if path.rstrip("/") == "/edge/status" and method == "GET":
            await self._json(send, 200, self._state())
            return
        if path.rstrip("/") == "/edge/mode" and method == "POST":
            await self._json(send, 200, await self._set_mode(await self._read_body(receive)))
            return
        await self._json(
            send,
            404,
            {"error": {"message": "not found", "type": "invalid_request_error"}},
        )

    async def _set_mode(self, body: bytes) -> dict[str, Any]:
        wanted = ""
        try:
            payload = json.loads(body) if body else {}
            wanted = str(payload.get("mode") or "").strip().lower()
        except (ValueError, UnicodeDecodeError):
            wanted = ""
        if wanted not in ("auto", "primary", "fallback"):
            return {
                "error": {
                    "message": "mode must be auto|primary|fallback",
                    "type": "invalid_request_error",
                },
                "mode": self._mode,
            }
        previous, self._mode = self._mode, wanted
        logger.warning(
            "edge: mode %s -> %s (manual)", previous, wanted, extra={"mode": wanted}
        )
        return {"mode": self._mode, "previous": previous, "route": self._preferred_route()}

    def _admin_ok(self, raw_headers: list[Any]) -> bool:
        key = self._settings.admin_key
        if not key:
            return False
        given = header_value(raw_headers, "x-admin-key") or header_value(
            raw_headers, "authorization"
        )
        if given.lower().startswith("bearer "):
            given = given[7:]
        return hmac.compare_digest(given.strip(), key)

    # ------------------------------------------------------------- helpers

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self._settings.connect_timeout_s,
                    read=None,  # generations can idle for minutes
                    write=60.0,
                    pool=10.0,
                ),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            )
        return self._client

    async def _read_body(self, receive: Receive) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _ClientGone
            if message["type"] != "http.request":
                continue
            chunk = message.get("body") or b""
            size += len(chunk)
            if size > self._settings.max_body_bytes:
                raise _BodyTooLarge
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    async def _json(self, send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    async def _aclose(resp: httpx.Response) -> None:
        try:
            await resp.aclose()
        except Exception:  # noqa: BLE001 - already closed by a disconnect
            pass


def create_edge_app() -> EdgeApp:
    """Factory used by ``python -m proxy_opencode.edge`` and uvicorn."""

    return EdgeApp(load_edge_settings())


__all__ = ["EdgeApp", "create_edge_app"]
