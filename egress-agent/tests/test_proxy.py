"""End-to-end tests for the agent's listener: CONNECT tunnel, healthz,
auth and the /rotate control endpoint (all over loopback).

All listeners use ephemeral ports (0) and the tests read the bound port
back from the server socket: fixed ports make the suite hostage to stale
listeners left behind by previously killed runs (Windows SO_REUSEADDR lets
a second bind succeed while connections keep going to the dead one — an
intermittent hang that cost hours to pin down).
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from egress_agent.config import AgentConfig
from egress_agent.egress import listen_socket
from egress_agent.health import HealthState
from egress_agent.proxy import ProxyServer


def _bound_port(server: asyncio.Server) -> int:
    return int(server.sockets[0].getsockname()[1])


async def _start_echo() -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, sock=listen_socket("127.0.0.1", 0))


def _config(**overrides) -> AgentConfig:
    data = {
        "listener": {"host": "127.0.0.1", "port": 0},
        "egress": {"name": "test-egress"},
        "echo_url": "http://127.0.0.1:1/echo",  # never polled in these tests
    }
    data.update(overrides)
    return AgentConfig.from_mapping(data)


async def _start_agent(config: AgentConfig) -> tuple[ProxyServer, HealthState]:
    state = HealthState(
        egress_name=config.egress.name,
        bind_source=config.egress.bind_source,
        echo_url=config.echo_url,
    )
    server = ProxyServer(config, state)
    await server.start()
    return server, state


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":")[1])
    raise AssertionError("response has no content-length")


async def _stop(server: asyncio.Server, timeout: float = 5.0) -> None:
    """Close a server without ever blocking the test on platform quirks."""

    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=timeout)
    except TimeoutError:
        pass  # leaked listener in the test process; the OS reclaims at exit


async def _send_recv(port: int, request: bytes) -> tuple[bytes, bytes]:
    """Send one request, return (head, body). Closes the client."""

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        length = _content_length(head)
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
        return head, body
    finally:
        writer.close()


async def _connect_through(agent_port: int, target_port: int, *, auth: str | None = None
                           ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", agent_port)
    request = (
        f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{target_port}\r\n"
    )
    if auth is not None:
        request += f"Proxy-Authorization: Basic {auth}\r\n"
    request += "\r\n"
    writer.write(request.encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    return reader, writer, head


@pytest.fixture
async def echo_server():
    server = await _start_echo()
    yield server
    await _stop(server)


@pytest.mark.asyncio
async def test_connect_tunnel_relays_bytes(echo_server) -> None:
    agent, state = await _start_agent(_config())
    try:
        reader, writer, head = await _connect_through(agent.port, _bound_port(echo_server))
        assert head.startswith(b"HTTP/1.1 200")
        payload = b"ping-through-tunnel"
        writer.write(payload)
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5)
        assert echoed == payload
        # counters are deterministic while the tunnel is up
        assert state.tunnels_active == 1
        assert state.tunnels_total == 1
        writer.transport.abort()
    finally:
        # bounded shutdown must never hang, whatever the platform delivers
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_connect_to_dead_port_is_502() -> None:
    agent, _state = await _start_agent(_config())
    try:
        reader, writer, head = await _connect_through(agent.port, 1)
        assert head.startswith(b"HTTP/1.1 502"), head
        writer.transport.abort()
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_healthz_endpoint_returns_payload() -> None:
    agent, _state = await _start_agent(_config())
    try:
        head, body = await _send_recv(
            agent.port, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        assert b"200" in head.split(b"\r\n")[0]
        payload = json.loads(body)
        assert payload["egress"] == "test-egress"
        assert payload["ready"] is True
        assert "tunnels_active" in payload
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_connect_without_auth_is_407() -> None:
    config = _config(listener={"host": "127.0.0.1", "port": 0,
                               "username": "u", "password": "p"})
    agent, _state = await _start_agent(config)
    try:
        head, _body = await _send_recv(
            agent.port, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        assert b"407" in head.split(b"\r\n")[0]
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_connect_with_correct_auth_tunnels(echo_server) -> None:
    config = _config(listener={"host": "127.0.0.1", "port": 0,
                               "username": "u", "password": "p"})
    agent, _state = await _start_agent(config)
    try:
        auth = base64.b64encode(b"u:p").decode()
        reader, writer, head = await _connect_through(
            agent.port, _bound_port(echo_server), auth=auth
        )
        assert head.startswith(b"HTTP/1.1 200")
        payload = b"authed"
        writer.write(payload)
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5)
        assert echoed == payload
        writer.transport.abort()
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_plain_http_and_unknown_paths_are_405() -> None:
    agent, _state = await _start_agent(_config())
    try:
        for request in (
            b"GET /anything HTTP/1.1\r\nHost: x\r\n\r\n",
            b"POST /rotatex HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
            b"GET http://opencode.ai/ HTTP/1.1\r\nHost: opencode.ai\r\n\r\n",
        ):
            head, _body = await _send_recv(agent.port, request)
            assert b"405" in head.split(b"\r\n")[0], request
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_rotate_unknown_ont_404() -> None:
    agent, _state = await _start_agent(_config(onts=[{"name": "a", "password": "x"}]))
    try:
        body = json.dumps({"ont": "nope"}).encode()
        head = (
            "POST /rotate HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            "Content-Length: " + str(len(body)) + "\r\n\r\n"
        ).encode()
        head_resp, resp_body = await _send_recv(agent.port, head + body)
        assert b"404" in head_resp.split(b"\r\n")[0]
        payload = json.loads(resp_body)
        assert payload["ok"] is False
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_rotate_no_onts_configured_409() -> None:
    agent, _state = await _start_agent(_config())
    try:
        body = json.dumps({}).encode()
        head = (
            "POST /rotate HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            "Content-Length: " + str(len(body)) + "\r\n\r\n"
        ).encode()
        head_resp, resp_body = await _send_recv(agent.port, head + body)
        assert b"409" in head_resp.split(b"\r\n")[0]
        assert json.loads(resp_body)["ok"] is False
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_rotate_accepts_plain_authorization_header(monkeypatch) -> None:
    """The gateway calls /rotate like any REST call: Authorization, not
    Proxy-Authorization (a curl -u bug hid a 407 as an empty reply)."""

    from egress_agent.rotate import RotationResult

    async def fake_restart(ont, *, wait=False):
        return RotationResult(ok=True, ont=ont.name, detail="mock")

    monkeypatch.setattr("egress_agent.rotate.restart_ont", fake_restart)
    config = _config(
        onts=[{"name": "hg8020c", "password": "x"}],
        listener={"host": "127.0.0.1", "port": 0, "username": "u", "password": "p"},
    )
    agent, _state = await _start_agent(config)
    try:
        body = json.dumps({"ont": "hg8020c"}).encode()
        head = (
            "POST /rotate HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            "Authorization: Basic " + base64.b64encode(b"u:p").decode() + "\r\n"
            "Content-Length: " + str(len(body)) + "\r\n\r\n"
        ).encode()
        head_resp, resp_body = await _send_recv(agent.port, head + body)
        assert b"200" in head_resp.split(b"\r\n")[0]
        assert json.loads(resp_body)["ok"] is True
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)


@pytest.mark.asyncio
async def test_rotate_calls_driver_for_configured_ont(monkeypatch) -> None:
    from egress_agent.rotate import RotationResult

    calls: list[str] = []

    async def fake_restart(ont, *, wait=False):
        calls.append(ont.name)
        return RotationResult(ok=True, ont=ont.name, detail="mock")

    monkeypatch.setattr("egress_agent.rotate.restart_ont", fake_restart)
    agent, state = await _start_agent(_config(onts=[{"name": "hg8020c", "password": "x"}]))
    try:
        body = json.dumps({"ont": "hg8020c"}).encode()
        head = (
            "POST /rotate HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            "Content-Length: " + str(len(body)) + "\r\n\r\n"
        ).encode()
        head_resp, resp_body = await _send_recv(agent.port, head + body)
        assert b"200" in head_resp.split(b"\r\n")[0]
        payload = json.loads(resp_body)
        assert payload["ok"] is True
        assert payload["results"][0]["ont"] == "hg8020c"
        assert calls == ["hg8020c"]
        assert state.last_rotation_ok is True
    finally:
        await asyncio.wait_for(agent.close(), timeout=10)
