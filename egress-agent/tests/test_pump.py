"""Deterministic tests for the duplex pump (no sockets involved).

The socket-level e2e tests in test_proxy.py depend on the platform
delivering close events; this module tests the pump's logic against
in-memory streams, which is where the relay semantics actually live.
"""

from __future__ import annotations

import asyncio

import pytest

from egress_agent.proxy import _pump


class FakeTransport:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.data += payload

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name, default=None):
        return default


class FakeProtocol(asyncio.Protocol):
    def _get_write_buffer_size(self) -> int:
        return 0

    async def _drain_helper(self) -> None:
        return None


def _streams():
    """Two reader/writer pairs backed by in-memory buffers."""

    pairs = []
    for _ in range(2):
        reader: asyncio.StreamReader = asyncio.StreamReader()
        protocol = FakeProtocol()
        transport = FakeTransport()
        writer = asyncio.StreamWriter(transport, protocol, reader, asyncio.get_event_loop())
        pairs.append((reader, writer, transport))
    return pairs[0], pairs[1]


@pytest.mark.asyncio
async def test_relays_both_directions_until_eof() -> None:
    (c_reader, c_writer, _), (u_reader, u_writer, _) = _streams()

    pump = asyncio.create_task(_pump(c_reader, c_writer, u_reader, u_writer))
    await asyncio.sleep(0)

    c_reader.feed_data(b"ping")
    await asyncio.sleep(0.05)
    u_reader.feed_data(b"pong")
    await asyncio.sleep(0.05)

    # client->up direction: the fake transport on the up writer holds the bytes
    assert u_writer.transport.data == b"ping"  # type: ignore[attr-defined]
    assert c_writer.transport.data == b"pong"  # type: ignore[attr-defined]

    c_reader.feed_eof()
    await asyncio.wait_for(pump, timeout=2)
    assert c_writer.transport.closed  # type: ignore[attr-defined]
    assert u_writer.transport.closed  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upstream_eof_also_tears_down() -> None:
    (c_reader, c_writer, _), (u_reader, u_writer, _) = _streams()

    pump = asyncio.create_task(_pump(c_reader, c_writer, u_reader, u_writer))
    await asyncio.sleep(0)
    u_reader.feed_eof()
    await asyncio.wait_for(pump, timeout=2)
    assert c_writer.transport.closed  # type: ignore[attr-defined]
    assert u_writer.transport.closed  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_error_on_one_side_tears_down() -> None:
    (c_reader, c_writer, _), (u_reader, u_writer, _) = _streams()

    pump = asyncio.create_task(_pump(c_reader, c_writer, u_reader, u_writer))
    await asyncio.sleep(0)
    c_reader.set_exception(ConnectionResetError("peer reset"))
    await asyncio.wait_for(pump, timeout=2)
    assert c_writer.transport.closed  # type: ignore[attr-defined]
    assert u_writer.transport.closed  # type: ignore[attr-defined]
