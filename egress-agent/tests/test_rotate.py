"""Tests for the Huawei ONT restart driver against a mock ONT.

The mock reproduces the exact production quirks the driver must handle:
UTF-8 BOM on the token endpoint, the "Waiting" login-success marker, the
onttoken embedded in the reset page, and the reset POST dropping the
connection (which is the success signal).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
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


class MockCtOnt:
    """China-Telecom e8-C firmware simulator (HG8020C, JSCT build).

    Quirks reproduced from the 2026-10-17 live capture: login is judged by a
    stateless credential Cookie header (no Set-Cookie anywhere), success/
    refusal is encoded in the Waiting page's pageName ('index.asp' vs '/'),
    the armed sequence is / -> /index.asp -> /frame.asp -> a fresh token,
    and the reset page embeds ``hwonttoken`` (32 hex, not 40+).
    """

    def __init__(self, *, login_ok: bool = True, drop_on_reset: bool = True,
                 serve_reset_page: bool = True) -> None:
        self.login_ok = login_ok
        self.drop_on_reset = drop_on_reset
        self.serve_reset_page = serve_reset_page
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> "MockCtOnt":
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

    def _page(self, path: str) -> tuple[int, bytes]:
        if path.startswith("/asp/GetRandCount.asp"):
            return 200, f"\ufeff{TOKEN[:32]}".encode()
        if path.startswith("/login.cgi"):
            name = "index.asp" if self.login_ok else "/"
            body = (
                "<!DOCTYPE html><html><head><title>Waiting...</title>"
                f"<script>var pageName = '{name}';</script></head></html>"
            )
            return 200, body.encode()
        if path.startswith("/CtcApp/e8c_restnorm.asp"):
            if self.serve_reset_page:
                page = (f'<input type="hidden" name="onttoken" '
                        f'id="hwonttoken" value="{ONTTOKEN}">')
                return 200, page.encode()
            return 404, b"The requested URL was not found on this server."
        return 200, b"ok"

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
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
        self.calls.append((method, target, dict(headers), body))

        status, payload = self._page(target)
        if target.startswith("/CtcApp/set.cgi"):
            if self.drop_on_reset:
                writer.close()  # the board goes down mid-reboot: success
                return
            status, payload = 200, b""
        head = (
            f"HTTP/1.1 {status} x\r\nContent-Type: text/html\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(head + payload)
        await writer.drain()
        writer.close()


def _ct_ont(url: str) -> OntConfig:
    return OntConfig(name="mock-ct", kind="huawei_ct", url=url,
                     username="useradmin", password="ct-pw")


def _expect_ct_cookie(ont: MockCtOnt) -> str:
    pwd_b64 = base64.b64encode(b"ct-pw").decode()
    return ("Cookie=UserName:useradmin:PassWord:"
            f"{pwd_b64}:Language:chinese:SubmitType=Login:id=-1")


CT_RESET = "/CtcApp/set.cgi"


@pytest.mark.asyncio
async def test_ct_restart_happy_path_carries_credential_cookie() -> None:
    ont_mock = await MockCtOnt().start()
    try:
        result = await restart_ont(_ct_ont(ont_mock.url), wait=False)
        assert result.ok is True
        steps = [t.split("?")[0] for _, t, _, _ in ont_mock.calls]
        assert steps == ["/asp/GetRandCount.asp", "/login.cgi", "/", "/index.asp",
                         "/frame.asp", "/asp/GetRandCount.asp",
                         "/CtcApp/e8c_restnorm.asp", CT_RESET]
        # the credential rides the Cookie header, not the form
        login_headers = ont_mock.calls[1][2]
        assert login_headers["cookie"] == _expect_ct_cookie(ont_mock)
        login = _form(ont_mock.calls[1][3])
        assert login["x.X_HW_Token"] == TOKEN[:32]
        # the armed sequence re-fetched a fresh token before the reset page
        # and the reset posted the page's 32-hex hwonttoken
        reset = _form(ont_mock.calls[-1][3])
        assert reset["x.X_HW_Token"] == ONTTOKEN
        reset_headers = ont_mock.calls[-1][2]
        assert reset_headers["cookie"] == _expect_ct_cookie(ont_mock)
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_ct_restart_login_refused_is_reported(monkeypatch) -> None:
    monkeypatch.setattr("egress_agent.rotate.CT_ARM_ATTEMPTS", 1)
    ont_mock = await MockCtOnt(login_ok=False).start()
    try:
        result = await restart_ont(_ct_ont(ont_mock.url), wait=False)
        assert result.ok is False
        assert "login refused" in result.detail
        steps = [t.split("?")[0] for _, t, _, _ in ont_mock.calls]
        assert "/CtcApp/e8c_restnorm.asp" not in steps
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_ct_restart_retries_arm_when_reset_page_404s(monkeypatch) -> None:
    monkeypatch.setattr("egress_agent.rotate.CT_ARM_ATTEMPTS", 2)
    ont_mock = await MockCtOnt(serve_reset_page=False).start()
    try:
        # the gate keeps 404ing -> cycle retries exhaust -> reported, no reset
        result = await restart_ont(_ct_ont(ont_mock.url), wait=False)
        assert result.ok is False
        assert "onttoken not found" in result.detail
        logins = [t for _, t, _, _ in ont_mock.calls if t.startswith("/login.cgi")]
        assert len(logins) == 2
        assert not any(t.startswith(CT_RESET) for _, t, _, _ in ont_mock.calls)
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_ct_restart_reset_without_drop_is_still_ok() -> None:
    ont_mock = await MockCtOnt(drop_on_reset=False).start()
    try:
        result = await restart_ont(_ct_ont(ont_mock.url), wait=False)
        assert result.ok is True
    finally:
        await ont_mock.stop()


class MockFenghuoOnt:
    """FiberHome Unicom simulator (登录握手 / reboot, 2026-10-17 live shape)."""

    def __init__(self, *, login_ok: bool = True, reboot_ok: bool = True,
                 reboot_torn: bool = False) -> None:
        self.login_ok = login_ok
        self.reboot_ok = reboot_ok
        self.reboot_torn = reboot_torn
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []
        self.server: asyncio.Server | None = None
        self.port = 0
        self.sid_seq = 0

    async def start(self) -> "MockFenghuoOnt":
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

    def _reply(self, payload: bytes, *, set_cookie: str | None = None) -> bytes:
        head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        if set_cookie:
            head += f"Set-Cookie: {set_cookie}; Path=/\r\n"
        head += f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        return head.encode() + payload

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, _ = lines[0].decode().split(" ", 2)
        headers: dict[str, str] = {}
        for raw in lines[1:]:
            if b":" in raw:
                k, _, v = raw.partition(b":")
                headers[k.decode().strip().lower()] = v.decode().strip()
        body = b""
        if headers.get("content-length"):
            body = await reader.readexactly(int(headers["content-length"]))
        self.calls.append((method, target, dict(headers), body))
        form = {k: v for k, v in parse_qs(body.decode()).items()}
        action = (form.get("ajaxmethod", [""])[0]
                  or dict(parse_qs(target.split("?")[-1])).get("ajaxmethod", [""])[0])

        if action == "get_login_user" or "ajaxmethod=get_login_user" in target:
            self.sid_seq += 1
            self.sid = f"sid{self.sid_seq:03d}"
            payload = json.dumps({"sessionid": self.sid, "random_string": "rs"}).encode()
        elif action == "do_login":
            result = {"login_result": 0 if self.login_ok else 4}
            payload = json.dumps({"random_string": "rs", "session_valid": 1,
                                  **result}).encode()
            cookie = "logintoken=abc123" if self.login_ok else None
            writer.write(self._reply(payload, set_cookie=cookie))
            await writer.drain()
            writer.close()
            return
        elif action == "reboot":
            if getattr(self, "reboot_torn", False):
                # board dies mid-response: bogus status + truncated body
                writer.write(b"HTTP/1.1 500 x\r\nContent-Length: 999\r\n"
                             b"Connection: close\r\n\r\ntorn")
                await writer.drain()
                writer.close()
                return
            payload = json.dumps({"random_string": "rs", "session_valid": 1,
                                  "success": "true" if self.reboot_ok else "false"}).encode()
        else:
            writer.write(self._reply(b'{"error": "unknown"}'))
            await writer.drain()
            writer.close()
            return
        writer.write(self._reply(payload))
        await writer.drain()
        writer.close()


def _feng_ont(url: str) -> OntConfig:
    return OntConfig(name="mock-feng", kind="fenghuo", url=url,
                     username="user", password="fh-pw")


@pytest.mark.asyncio
async def test_fenghuo_restart_happy_path() -> None:
    ont_mock = await MockFenghuoOnt().start()
    try:
        result = await restart_ont(_feng_ont(ont_mock.url), wait=False)
        assert result.ok is True
        posts = [(t, _form(b)) for m, t, _, b in ont_mock.calls if m == "POST"]
        all_targets = [u.split("?")[0] for _, u, _, _ in ont_mock.calls]
        assert set(all_targets) == {"/cgi-bin/ajax"}
        assert len(posts) == 2
        _t, fields0 = posts[0]
        _t, fields1 = posts[1]
        assert fields0["ajaxmethod"] == "do_login"
        assert fields0["username"] == "user"
        assert fields0["loginpd"] == hashlib.sha256(b"fh-pw").hexdigest()
        assert fields0["sessionid"].startswith("sid")
        assert fields1["ajaxmethod"] == "reboot"
        assert fields1["sessionid"].startswith("sid002")
        # the logintoken cookie was forwarded on the reboot post
        reboot_headers = ont_mock.calls[-1][2]
        assert reboot_headers["cookie"] == "logintoken=abc123"
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_fenghuo_login_refused_is_reported() -> None:
    ont_mock = await MockFenghuoOnt(login_ok=False).start()
    try:
        result = await restart_ont(_feng_ont(ont_mock.url), wait=False)
        assert result.ok is False
        assert "login refused" in result.detail
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_fenghuo_reboot_not_confirmed_is_reported() -> None:
    ont_mock = await MockFenghuoOnt(reboot_ok=False).start()
    try:
        result = await restart_ont(_feng_ont(ont_mock.url), wait=False)
        assert result.ok is False
        assert "reboot not confirmed" in result.detail
    finally:
        await ont_mock.stop()


@pytest.mark.asyncio
async def test_fenghuo_reboot_mangled_response_still_ok() -> None:
    """The board resets mid-response in the wild; any mangled reply counts."""
    ont_mock = await MockFenghuoOnt().start()
    # override: reboot POST answers HTTP 500 with a torn non-JSON body
    ont_mock.reboot_torn = True
    try:
        result = await restart_ont(_feng_ont(ont_mock.url), wait=False)
        assert result.ok is True
    finally:
        await ont_mock.stop()
