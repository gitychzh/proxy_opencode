"""Device-side proxy data plane: one port, three duties.

The agent listens on a single port and speaks just enough HTTP to serve:

* ``CONNECT host:port`` — the tunnel the gateway's egress pool uses. After
  the 200, both directions are pumped as raw bytes: no payload inspection,
  no logging of targets (log red line).
* ``GET /healthz`` — unauthenticated status + observed egress IP (see
  ``health.py``); the gateway's pool polls it.
* ``POST /rotate`` — authenticated ONT reboot (see ``rotate.py``; the
  driver produces the IP change the gateway's 500-request counter asks
  for).

Why the listener is raw asyncio instead of aiohttp: the request line a
CONNECT client sends is authority-form (``CONNECT opencode.ai:443 HTTP/1.1``),
and how aiohttp maps that onto ``request.path`` is not a stable public
contract — the proxy semantics (half-duplex pump with source-bound
outbound sockets) are what this daemon exists to provide. Parsing the
fixed request head below is ~20 lines and fully specified by RFC 9110
§9.3.6; everything that is genuinely hard (HTTP client behaviour for the
probes) is delegated to aiohttp. Everything else (plain HTTP proxying, i.e.
absolute-URI GET forwarding) is refused with 405 — the gateway only ever
tunnels TLS.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from typing import Any

from .config import AgentConfig, ListenerConfig, OntConfig
from .egress import listen_socket, local_addr_for
from .health import HealthState

log = logging.getLogger("egress_agent.proxy")

MAX_HEAD_BYTES = 16 * 1024
PUMP_CHUNK = 64 * 1024
CLOSE_GRACE_S = 3.0
CLOSE_ABORT_S = 1.0

_BAD_REQUEST = (
    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)
_TOO_LARGE = (
    b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)
_UNAUTHORIZED = (
    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
    b'Proxy-Authenticate: Basic realm="egress-agent"\r\n'
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)
_METHOD_NOT_ALLOWED_BODY = b"CONNECT only (no plain HTTP relay)\n"
_METHOD_NOT_ALLOWED = (
    b"HTTP/1.1 405 Method Not Allowed\r\n"
    b"Content-Type: text/plain\r\nContent-Length: "
    + str(len(_METHOD_NOT_ALLOWED_BODY)).encode()
    + b"\r\nConnection: close\r\n\r\n"
    + _METHOD_NOT_ALLOWED_BODY
)


def _check_auth(
    headers: dict[str, str],
    listener: ListenerConfig,
    *,
    allow_authorization: bool = False,
) -> bool:
    """Shared-secret check.

    ``allow_authorization`` makes the plain ``Authorization`` header count
    too — right for control endpoints like ``/rotate`` (called by the
    gateway like any REST call); CONNECT tunnels keep the proxy convention
    of ``Proxy-Authorization`` only.
    """

    if not listener.username:
        return True  # auth not configured
    names = ["proxy-authorization"]
    if allow_authorization:
        names.append("authorization")
    presented = next((headers.get(n, "") for n in names if headers.get(n)), "")
    if not presented.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(presented[6:].strip(), validate=False).decode()
    except Exception:
        return False
    expected = f"{listener.username}:{listener.password or ''}"
    return hmac.compare_digest(decoded, expected)


class ProxyServer:
    """The agent's single-port listener (CONNECT + control endpoints)."""

    def __init__(self, config: AgentConfig, state: HealthState) -> None:
        self.config = config
        self.state = state
        self.listener = config.listener
        self.bind_source = config.egress.bind_source
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> asyncio.Server:
        # Pre-bound socket: see egress.listen_socket for why the loop's
        # getaddrinfo executor is bypassed.
        self._server = await asyncio.start_server(
            self._handle, sock=listen_socket(self.listener.host, self.listener.port)
        )
        log.info(
            "agent listening",
            extra={
                "listen": f"{self.listener.host}:{self.port}",
                "egress": self.state.egress_name,
                "bind": self.bind_source or "(OS routing)",
            },
        )
        return self._server

    @property
    def port(self) -> int:
        """Actually bound port (differs from the config when using 0)."""

        if self._server is None or not self._server.sockets:
            return self.listener.port
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        """Bounded shutdown: never let a stuck handler block the loop.

        The graceful path waits for live handlers to finish; if they do not
        (upstream close events can be delayed by the platform), the remaining
        tunnels are aborted and shutdown completes anyway — the agent must
        always be restartable by systemd within seconds.
        """

        if self._server is None:
            return
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=CLOSE_GRACE_S)
            return
        except TimeoutError:
            log.error(
                "shutdown grace expired; aborting live tunnels",
                extra={"tunnels_active": self.state.tunnels_active},
            )
        for writer in list(self._writers):
            try:
                writer.transport.abort()
            except Exception:
                pass
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=CLOSE_ABORT_S)
        except TimeoutError:
            log.error("handler teardown incomplete; proceeding anyway")
        finally:
            self._writers.clear()

    # ------------------------------------------------------------- dispatch

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._writers.add(writer)
        try:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                writer.close()
                return
            except asyncio.LimitOverrunError:
                writer.write(_TOO_LARGE)
                await writer.drain()
                writer.close()
                return
            lines = head.split(b"\r\n")
            try:
                method, target, _version = lines[0].decode("latin-1").split(" ", 2)
            except ValueError:
                writer.write(_BAD_REQUEST)
                await writer.drain()
                writer.close()
                return
            headers: dict[str, str] = {}
            for raw in lines[1:]:
                if b":" in raw:
                    key, _, value = raw.partition(b":")
                    headers[key.decode("latin-1").strip().lower()] = (
                        value.decode("latin-1").strip()
                    )

            if method == "CONNECT":
                await self._handle_connect(target, headers, reader, writer, peer)
            elif method == "GET" and target.rstrip("/") in ("/healthz", "/health"):
                await self._respond_json(writer, self.state.payload())
            elif method == "POST" and target.rstrip("/") == "/rotate":
                await self._handle_rotate(headers, reader, writer)
            else:
                writer.write(_METHOD_NOT_ALLOWED)
                await writer.drain()
                writer.close()
        except Exception:
            log.exception("connection handling failed", extra={"peer": str(peer)})
            try:
                writer.close()
            except Exception:
                pass
        finally:
            self._writers.discard(writer)

    # --------------------------------------------------------------- CONNECT

    async def _handle_connect(
        self,
        target: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: Any,
    ) -> None:
        if not _check_auth(headers, self.listener):
            writer.write(_UNAUTHORIZED)
            await writer.drain()
            writer.close()
            return
        host, port = _split_authority(target)
        if host is None or port is None:
            writer.write(_BAD_REQUEST)
            await writer.drain()
            writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(
                host, port, local_addr=local_addr_for(self.bind_source)
            )
        except Exception as exc:
            log.warning(
                "upstream connect failed",
                extra={"error": type(exc).__name__},  # never log the target
            )
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        self.state.tunnels_active += 1
        self.state.tunnels_total += 1
        log.info(
            "tunnel open",
            extra={"egress": self.state.egress_name, "peer": str(peer)},
        )
        try:
            await _pump(reader, writer, up_reader, up_writer)
        finally:
            self.state.tunnels_active -= 1
            log.info(
                "tunnel closed",
                extra={
                    "egress": self.state.egress_name,
                    "tunnels_active": self.state.tunnels_active,
                    "tunnels_total": self.state.tunnels_total,
                },
            )

    # -------------------------------------------------------------- /rotate

    async def _handle_rotate(
        self,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not _check_auth(headers, self.listener, allow_authorization=True):
            writer.write(_UNAUTHORIZED)
            await writer.drain()
            writer.close()
            return
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        body = await reader.readexactly(length) if 0 < length <= 4096 else b"{}"
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            request = {}
        wanted = request.get("ont")
        from .rotate import restart_ont  # local import: rotate owns its deps

        onts: list[OntConfig] = list(self.config.onts)
        if wanted:
            onts = [o for o in onts if o.name == wanted]
            if not onts:
                await self._respond_json(
                    writer, {"ok": False, "detail": f"unknown ont {wanted!r}"}, status=404
                )
                return
        if not onts:
            await self._respond_json(
                writer, {"ok": False, "detail": "no onts configured"}, status=409
            )
            return

        results = []
        try:
            for ont in onts:
                result = await restart_ont(ont, wait=bool(request.get("wait")))
                results.append(result.as_dict())
                self.state.note_rotation(result.ok)
        except Exception as exc:
            log.exception("rotate failed", extra={"error": type(exc).__name__})
            await self._respond_json(
                writer, {"ok": False, "detail": f"rotate failed: {exc}"}, status=500
            )
            return
        await self._respond_json(writer, {"ok": all(r["ok"] for r in results),
                                          "results": results})

    # ------------------------------------------------------------- helpers

    async def _respond_json(
        self, writer: asyncio.StreamWriter, payload: dict[str, object], status: int = 200
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        head = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(head + body)
        await writer.drain()
        writer.close()


async def _pump(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    """Relay bytes until either side closes, then tear everything down."""

    async def _oneway(src: asyncio.StreamReader, dst: asyncio.StreamWriter, tag: str) -> None:
        try:
            while True:
                chunk = await src.read(PUMP_CHUNK)
                log.debug("oneway read", extra={"tag": tag, "bytes": len(chunk)})
                if not chunk:
                    log.debug("oneway eof", extra={"tag": tag})
                    break
                dst.write(chunk)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            log.debug("oneway error", extra={"tag": tag, "error": type(exc).__name__})

    task_down = asyncio.create_task(
        _oneway(client_reader, up_writer, "client->up")
    )
    task_up = asyncio.create_task(
        _oneway(up_reader, client_writer, "up->client")
    )
    try:
        await asyncio.wait({task_down, task_up}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (task_down, task_up):
            task.cancel()
        for stream_writer in (client_writer, up_writer):
            try:
                stream_writer.close()
            except Exception:
                pass


def _split_authority(target: str) -> tuple[str | None, int | None]:
    """``host:port`` (or ``[v6]:port``) → (host, port); (None, None) on garbage."""

    if not target:
        return None, None
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0 or not target[closing + 1:].startswith(":"):
            return None, None
        return target[1:closing], _safe_port(target[closing + 2:])
    host, sep, port = target.rpartition(":")
    if not sep:
        return None, None
    return host, _safe_port(port)


def _safe_port(text: str) -> int | None:
    try:
        port = int(text)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None
