"""Outbound source-address binding helpers.

One agent instance fronts exactly one uplink, so every outbound socket
must leave through that uplink's address even when the OS default route
points elsewhere (opc2: default route is wifi, but the wired bucket must
egress via the wired address — same trick the old gateway used with its
uid policy routing, here it is explicit).

* ``interface_address`` — resolve a Linux interface name to its IPv4
  (``SIOCGIFADDR``). Used to reach the right ONT when two ONTs share one
  IP (opc2: wired V173-50 vs wireless HG8020C both on 192.168.1.1 — the
  source address is the only reliable discriminator, exactly like the
  ``curl --interface`` method proven in production).
* ``local_addr_for`` — the ``local_addr`` tuple ``asyncio.open_connection``
  and ``aiohttp.TCPConnector`` want, or ``None`` for OS routing.

``SO_BINDTODEVICE`` is deliberately NOT used: it needs CAP_NET_RAW, and the
agent must be able to run as an unprivileged service user.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import sys
from typing import Any

# ioctl request number for SIOCGIFADDR on Linux.
_SIOCGIFADDR = 0x8915


def interface_address(name: str) -> str | None:
    """IPv4 address of a Linux interface, or ``None`` if unavailable.

    Non-Linux platforms return ``None`` (Windows/Android builds use OS
    routing; see deploy/windows/ and egress-agent-android/).
    """

    if sys.platform != "linux" or not name:
        return None
    import fcntl  # Linux-only module; imported lazily on purpose

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(
            sock.fileno(), _SIOCGIFADDR, struct.pack("256s", name[:15].encode())
        )
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def local_addr_for(bind_source: str | None) -> tuple[str, int] | None:
    """``local_addr`` argument for asyncio/aiohttp outbound connections."""

    if not bind_source:
        return None
    return (bind_source, 0)


def listen_socket(host: str, port: int) -> socket.socket:
    """Pre-bound, listening, non-blocking socket for ``start_server(sock=...)``.

    Binding synchronously (instead of letting ``asyncio.start_server`` resolve
    the host through the loop's getaddrinfo executor) keeps startup
    deterministic: the agent binds numeric addresses only, and the loop's
    resolver thread pool is a known source of intermittent hangs under test
    runners that recycle loops (pytest-asyncio) on Windows.
    """

    host = host or "0.0.0.0"
    family = socket.AF_INET
    sockaddr: Any = (host, port)
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.version == 6:
        family = socket.AF_INET6
        sockaddr = (host, port, 0, 0)
    elif parsed is None:
        info = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
        family, _type, _proto, _canon, sockaddr = info[0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(sockaddr)
    sock.listen(256)
    sock.setblocking(False)
    return sock


def describe_bind(bind_source: str | None) -> dict[str, Any]:
    """Secret-free bind description for logs and /healthz payloads."""

    return {"bind_source": bind_source or "(OS routing)"}
