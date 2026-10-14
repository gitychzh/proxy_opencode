"""Tests for the Huawei ONT restart driver against a mock ONT.

The mock reproduces the exact production quirks the driver must handle:
UTF-8 BOM on the token endpoint, the "Waiting" login-success marker, the
onttoken embedded in the reset page, and the reset POST dropping the
connection (which is the success signal).
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import parse_qs

import pytest

from egress_agent.config import OntConfig
from egress_agent.rotate import restart_ont

TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
ONTTOKEN = "0123456789abcdef0123456789abcdef01234567"
RESET_PAGE = "/html/ssmp/devmanage/cmccdevicereset.asp"


class MockOnt:
    """Minimal Huawei-style ONT simulator with a request recorder."""

    def __init__(self, *, login_ok: bool = True, drop_on_reset: bool = True) -> None:
        self.login_ok = login_ok
        self.drop_on_reset = drop_on_reset
        self.calls: list[tuple[str, str, bytes]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> "MockOnt":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, _ = lines[0].decode().split(" ", 2)
        headers: dict[str, str] = {}
        for raw in lines[1:]:
            if b":" in raw:
                k, _, v = raw.partition(b":")
                headers[k.decode().strip().lower()] = v.decode().strip()
        body = b""
        if "content-length" in headers:
            body = await reader.readexactly(int(headers["content-length"]))
        self.calls.append((method, target, body))

        if target.startswith("/asp/GetRandCount.asp"):
            payload = f"\ufeff{TOKEN}".encode()  # the notorious BOM
            head = (
                f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head + payload)
        elif target.startswith("/login.cgi"):
            body_text = (
                "<html>Waiting... top.location.replace('/')</html>"
                if self.login_ok
                else "<html>Login failed</html>"
            )
            encoded = body_text.encode()
            head = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                "Set-Cookie: CookieHttp=sid=deadbeef:Language:chinese:id=1;"
                " path=/; HttpOnly\r\n"
                "Content-Length: " + str(len(encoded)) + "\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head + encoded)
        elif target.startswith(RESET_PAGE):
            # Only a request carrying the session cookie gets the real page;
            # without it the ONT serves the login frame (no onttoken).
            cookie = headers.get("cookie", "")
            if "CookieHttp=" in cookie:
                page = (
                    f'<html><input type="hidden" name="onttoken" value="{ONTTOKEN}"></html>'
                ).encode()
            else:
                page = b"<html>Waiting... login frame, no token here</html>"
            head = (
                f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(page)}\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head + page)
        elif target.startswith("/set.cgi"):
            if self.drop_on_reset:
                writer.close()  # the reboot drops the connection: success
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        elif target.startswith("/"):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        else:
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        await writer.drain()
        writer.close()


def _form(body: bytes) -> dict[str, str]:
    fields = parse_qs(body.decode(), keep_blank_values=True)
    return {k: v[0] for k, v in fields.items()}


def _ont(url: str) -> OntConfig:
    return OntConfig(name="mock", kind="huawei", url=url, username="user",
                     password="pw#with-hash")


@pytest.mark.asyncio
async def test_restart_happy_path_bom_stripped_in_order() -> None:
    ont_mock = await MockOnt().start()
    try:
        result = await restart_ont(_ont(ont_mock.url), wait=False)
        assert result.ok is True
        steps = [t.split("?")[0] for _, t, _ in ont_mock.calls]
        assert steps == ["/asp/GetRandCount.asp", "/login.cgi", RESET_PAGE, "/set.cgi"]
        # the login carried the BOM-stripped token and the base64 password
        login = _form(ont_mock.calls[1][2])
        assert login["x.X_HW_Token"] == TOKEN
        assert login["UserName"] == "user"
        assert login["PassWord"] == base64.b64encode(b"pw#with-hash").decode()
        # the reset carried the page's onttoken
        reset = _form(ont_mock.calls[3][2])
        assert reset["x.X_HW_Token"] == ONTTOKEN
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_restart_login_refused_is_reported() -> None:
    ont_mock = await MockOnt(login_ok=False).start()
    try:
        result = await restart_ont(_ont(ont_mock.url), wait=False)
        assert result.ok is False
        assert "login refused" in result.detail
        assert len(ont_mock.calls) == 2  # token + login, no reset
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_restart_with_wait_polls_recovery(monkeypatch) -> None:
    monkeypatch.setattr("egress_agent.rotate.REDIAL_WAIT_S", 0.01)
    ont_mock = await MockOnt().start()
    try:
        result = await restart_ont(_ont(ont_mock.url), wait=True)
        assert result.ok is True
        assert "redialed" in result.detail
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_restart_reset_without_drop_is_still_ok() -> None:
    ont_mock = await MockOnt(drop_on_reset=False).start()
    try:
        result = await restart_ont(_ont(ont_mock.url), wait=False)
        assert result.ok is True
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_restart_unreachable_ont_is_reported() -> None:
    # port 1 on localhost: connection refused, no exception escapes
    result = await restart_ont(_ont("http://127.0.0.1:1"), wait=False)
    assert result.ok is False
    assert result.detail
