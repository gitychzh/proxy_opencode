"""Tests for egress_agent.egress (source binding)."""

from __future__ import annotations

import sys
from unittest import mock

import pytest

from egress_agent.egress import (
    describe_bind,
    interface_address,
    listen_socket,
    local_addr_for,
)


def test_local_addr_for_none() -> None:
    assert local_addr_for(None) is None
    assert local_addr_for("") is None


def test_local_addr_for_source() -> None:
    assert local_addr_for("192.168.1.222") == ("192.168.1.222", 0)


def test_interface_address_non_linux_returns_none() -> None:
    if sys.platform != "linux":
        assert interface_address("eth0") is None


@pytest.mark.skipif(sys.platform != "linux", reason="SIOCGIFADDR is Linux-only")
def test_interface_address_loopback_linux() -> None:
    assert interface_address("lo") == "127.0.0.1"


def test_interface_address_ioctl_failure_returns_none() -> None:
    if sys.platform != "linux":
        pytest.skip("ioctl path is Linux-only")
    with mock.patch("egress_agent.egress.fcntl.ioctl", side_effect=OSError):
        assert interface_address("nope0") is None


def test_describe_bind_never_leaks_secrets() -> None:
    assert describe_bind(None) == {"bind_source": "(OS routing)"}
    assert describe_bind("10.0.0.9") == {"bind_source": "10.0.0.9"}


def test_listen_socket_numeric_v4_binds_and_listens() -> None:
    sock = listen_socket("127.0.0.1", 0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.getsockname()[1] > 0
        assert sock.getblocking() is False
    finally:
        sock.close()


def test_listen_socket_empty_host_binds_all() -> None:
    sock = listen_socket("", 0)
    try:
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()
