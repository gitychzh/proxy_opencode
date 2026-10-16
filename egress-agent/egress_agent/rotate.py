"""Huawei ONT reboot driver — renew the egress IP (P1).

The four-step pure-HTTP flow below is the one already proven in production
on this hardware (see the opc2 runbooks): it needs no browser, no session
cookies and no firmware-specific CLI. The gateway triggers it through the
agent's ``POST /rotate``; the ONT password stays on the device.

Flow (all requests source-bound when the ONT config names an interface):

1. ``GET {url}/asp/GetRandCount.asp`` → token. The response carries a UTF-8
   BOM — strip it with ``str.removeprefix`` (a byte-level ``tr`` cannot
   remove a multi-byte sequence and corrupts the token; that exact bug
   caused 403s in the original script).
2. ``POST {url}/login.cgi`` with ``UserName``, base64 ``PassWord`` and
   ``x.X_HW_Token``. Success marker: HTTP 200 and a body containing
   ``Waiting``. Do NOT look for a ``sid`` cookie — some of these ONTs never
   set one even on success (the old script's ``sid=`` check produced false
   failures for exactly that reason).
3. ``GET {url}/html/ssmp/devmanage/cmccdevicereset.asp`` → the page embeds
   ``onttoken`` (40+ hex chars); hand-built form posts without it are
   rejected with "请求的操作不能执行".
4. ``POST {url}/set.cgi?x=...ResetBoard&RequestFile=html/ssmp/devmanage/
   cmccdevicereset.asp`` with the token. A dropped connection (or a
   transport error) at this point is the success signal — the ONT reboots
   immediately.

Recovery (``wait=True``): poll the ONT web root until it answers again
(~80 s observed), then wait for the ISP's PPPoE redial (~30-40 s). The
caller re-reads the egress IP from ``/healthz`` afterwards and verifies it
actually changed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import OntConfig
from .egress import interface_address, local_addr_for

log = logging.getLogger("egress_agent.rotate")

RESET_PATH = (
    "/set.cgi?x=InternetGatewayDevice.X_HW_DEBUG.SMP.DM.ResetBoard"
    "&RequestFile=html/ssmp/devmanage/cmccdevicereset.asp"
)
RESET_PAGE = "/html/ssmp/devmanage/cmccdevicereset.asp"
ONTTOKEN_RE = re.compile(r'value="([a-f0-9]{40,})"')

# --- China Telecom firmware (e8-C JSCT build, e.g. HG8020C), 2026-10-17 ---
# Live-verified sequence on the telecom HG8020C (_credentials ride a
# stateless "Cookie" header — the CT login response carries no Set-Cookie):
#   1. GET /asp/GetRandCount.asp -> token (BOM)
#   2. POST /login.cgi form x.X_HW_Token=<token> + credential Cookie;
#      success marker: body contains pageName = 'index.asp' ('/' = refused)
#   3. arm: GET / -> /index.asp -> /frame.asp -> /asp/GetRandCount.asp
#   4. GET /CtcApp/e8c_restnorm.asp -> hwonttoken decimal-hex value
#   5. POST /CtcApp/set.cgi?...ResetBoard&RequestFile=... with x.X_HW_Token
#      =<hwonttoken>; the handler hangs then drops the socket while the board
#      reboots (curl rc 28 / instant reset — both observed in the wild).
CT_RESET_PAGE = "/CtcApp/e8c_restnorm.asp"
CT_RESET_PATH = (
    "/CtcApp/set.cgi?x=InternetGatewayDevice.X_HW_DEBUG.SMP.DM.ResetBoard"
    "&RequestFile=CtcApp/e8c_restnorm.asp"
)
CT_ONTTOKEN_RE = re.compile(r'id="hwonttoken"\s+value="([0-9a-f]{20,64})"')
CT_ARM_ATTEMPTS = 2  # the CT session gate flaps (occasional 404 on step 4)

# --- FiberHome Unicom firmware (e.g. HG6142A-C), 2026-10-17 ---
# Session handshake via /cgi-bin/ajax, phone-side live-verified:
#   1. GET /cgi-bin/ajax?ajaxmethod=get_login_user&tkn=<UA>  ('tkn' is
#      REQUIRED — without it the server omits sessionid)
#      -> {"sessionid": "...", "random_string": "..."}
#   2. POST /cgi-bin/ajax urlencoded: username, loginpd = sha256(password)
#      (hex), port=0, sessionid=<sid>, ajaxmethod=do_login
#      -> {"login_result": 0} on success (4 = bad credentials)
#   3. fresh get_login_user again (the reboot page re-handshakes),
#      POST ajaxmethod=reboot&sessionid=<fresh_id>
#      -> {"success": "true"}; the ajax session cookie is 'logintoken'.
FENG_UA = ("Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
FENG_REBOOT_PAGE = "/html/resetrouter.html"

RECOVER_TIMEOUT_S = 180.0
REDIAL_WAIT_S = 40.0


class OntLoginError(RuntimeError):
    """The ONT refused the credentials (or the anti-brute-force lock)."""


@dataclass
class RotationResult:
    ok: bool
    ont: str
    detail: str = ""
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ont": self.ont,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 1),
        }


def ont_bind_source(ont: OntConfig) -> str | None:
    """Source address for reaching this ONT (interface wins over nothing)."""

    if ont.interface:
        return interface_address(ont.interface)
    return None


def _session(bind_source: str | None,
             extra_headers: dict[str, str] | None = None) -> aiohttp.ClientSession:
    """Session with the loop resolver bypassed and cookies handled manually.

    Huawei ONTs answer login with a malformed ``CookieHttp=sid:...:
    Language:chinese:id=1`` header that strict cookie jars drop on the
    floor (curl's jar stores it blindly — which is why the production
    script works and a naive client does not). We therefore disable the
    jar and forward the raw ``Cookie`` header ourselves, exactly like
    ``curl -b "$JAR"``.
    """

    connector = aiohttp.TCPConnector(local_addr=local_addr_for(bind_source))
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=15),
        cookie_jar=aiohttp.DummyCookieJar(),
        headers=extra_headers,
    )


async def restart_ont(
    ont: OntConfig,
    *,
    bind_source: str | None = None,
    wait: bool = False,
) -> RotationResult:
    """Reboot one ONT. Returns ok=False with a detail string on failure."""

    t0 = time.monotonic()
    bind = bind_source if bind_source is not None else ont_bind_source(ont)
    log.info("ont rotation start", extra={"ont": ont.name, "bind": bind})
    try:
        trigger = await _trigger_reboot(ont, bind)
    except OntLoginError as exc:
        return RotationResult(ok=False, ont=ont.name, detail=str(exc),
                              elapsed_s=time.monotonic() - t0)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        return RotationResult(
            ok=False, ont=ont.name,
            detail=f"ont unreachable: {type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )
    if wait:
        recovered = await _wait_recovered(ont, bind)
        if recovered:
            await asyncio.sleep(REDIAL_WAIT_S)
        return RotationResult(
            ok=trigger and recovered,
            ont=ont.name,
            detail="rebooted and redialed" if trigger and recovered
            else ("rebooted, redial wait skipped" if trigger else "reboot not confirmed"),
            elapsed_s=time.monotonic() - t0,
        )
    return RotationResult(
        ok=trigger,
        ont=ont.name,
        detail="reboot accepted (recovery not awaited)",
        elapsed_s=time.monotonic() - t0,
    )


async def _trigger_reboot(ont: OntConfig, bind: str | None) -> bool:
    """Login + fetch token + fire the reset. True when the reset was accepted."""

    if ont.kind == "huawei_ct":
        return await _trigger_reboot_ct(ont, bind)
    if ont.kind == "fenghuo":
        return await _trigger_reboot_fenghuo(ont, bind)
    return await _trigger_reboot_cmcc(ont, bind)


async def _trigger_reboot_cmcc(ont: OntConfig, bind: str | None) -> bool:
    """CMCC firmware flow (V173 et al.): form-login + Set-Cookie session."""

    session = _session(bind)
    try:
        # 1) random-count token (BOM must be stripped from the decoded text)
        async with session.get(f"{ont.url}/asp/GetRandCount.asp") as resp:
            if resp.status != 200:
                raise OntLoginError(f"GetRandCount status {resp.status}")
            token = (await resp.text()).removeprefix("\ufeff").strip()
        if not token:
            raise OntLoginError("empty GetRandCount token")

        # 2) login — password is base64-encoded, token rides the form
        pass_b64 = base64.b64encode(ont.password.encode()).decode()
        async with session.post(
            f"{ont.url}/login.cgi",
            data={"UserName": ont.username, "PassWord": pass_b64,
                  "x.X_HW_Token": token},
        ) as resp:
            body = await resp.text()
            if resp.status != 200 or "Waiting" not in body:
                raise OntLoginError(f"login refused (status {resp.status})")
            # The ONT's session cookie is malformed for strict parsers;
            # forward the raw name=value pair like curl -b does.
            set_cookie = resp.headers.get("Set-Cookie")
            cookie = set_cookie.split(";")[0].strip() if set_cookie else None
        headers = {"Cookie": cookie} if cookie else {}

        # 3) onttoken from the reset page (same session)
        async with session.get(f"{ont.url}{RESET_PAGE}", headers=headers) as resp:
            page = await resp.text()
        match = ONTTOKEN_RE.search(page)
        if not match:
            raise OntLoginError("onttoken not found on reset page")

        # 4) fire the reboot; a dropped connection here means success
        try:
            async with session.post(
                f"{ont.url}{RESET_PATH}",
                data={"x.X_HW_Token": match.group(1)},
                headers=headers,
            ) as resp:
                log.info("ont reboot accepted",
                         extra={"ont": ont.name, "status": resp.status})
                return True
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            log.info("ont connection dropped at reset (expected)")
            return True
    finally:
        await session.close()


def _ct_cookie_header(username: str, password: str) -> str:
    """The CT firmware authenticates via a stateless ``Cookie`` header."""
    pwd_b64 = base64.b64encode(password.encode()).decode()
    return (f"Cookie=UserName:{username}:PassWord:{pwd_b64}:"
            "Language:chinese:SubmitType=Login:id=-1")


async def _ct_get_token(session: aiohttp.ClientSession, ont: OntConfig,
                        headers: dict[str, str]) -> str:
    async with session.get(f"{ont.url}/asp/GetRandCount.asp",
                           headers=headers) as resp:
        if resp.status != 200:
            raise OntLoginError(f"GetRandCount status {resp.status}")
        token = (await resp.text()).removeprefix("\ufeff").strip()
    if not token:
        raise OntLoginError("empty GetRandCount token")
    return token


async def _trigger_reboot_ct(ont: OntConfig, bind: str | None) -> bool:
    """CT (e8-C JSCT) firmware flow: stateless credential cookie + arm sequence."""

    session = _session(bind)
    try:
        headers = {"Cookie": _ct_cookie_header(ont.username, ont.password)}
        # Full-cycle retries: the device's session gate intermittently refuses
        # the reset page (404) even right after a valid login.
        last_err: Exception | None = None
        for attempt in range(CT_ARM_ATTEMPTS):
            if attempt:
                await asyncio.sleep(2)
            try:
                return await _ct_reboot_once(session, ont, headers)
            except OntLoginError as exc:
                last_err = exc
        raise last_err  # type: ignore[misc]
    finally:
        await session.close()


async def _ct_reboot_once(session: aiohttp.ClientSession, ont: OntConfig,
                          headers: dict[str, str]) -> bool:
    # 1) login — token in the form, credentials in the Cookie header
    token = await _ct_get_token(session, ont, headers)
    async with session.post(
        f"{ont.url}/login.cgi", data={"x.X_HW_Token": token},
        headers={**headers, "Referer": f"{ont.url}/"},
    ) as resp:
        body = await resp.text()
        if resp.status != 200 or "index.asp" not in body:
            # refused logins answer pageName = '/'
            raise OntLoginError(
                f"login refused (status {resp.status}, no index.asp redirect)")
    # 2) arm the session the way the browser does after the Waiting redirect
    for path in ("/", "/index.asp", "/frame.asp"):
        async with session.get(f"{ont.url}{path}", headers=headers) as resp:
            await resp.read()
    await _ct_get_token(session, ont, headers)  # refresh per the page flow
    # 3) onttoken from the reset page
    async with session.get(f"{ont.url}{CT_RESET_PAGE}",
                           headers={**headers, "Referer": f"{ont.url}/index.asp"}) as resp:
        page = await resp.text()
    match = CT_ONTTOKEN_RE.search(page)
    if not match:
        raise OntLoginError(f"onttoken not found on reset page "
                            f"(page {len(page)} bytes)")
    # 4) fire the reboot; the handler hangs and drops the socket mid-reboot
    try:
        async with session.post(
            f"{ont.url}{CT_RESET_PATH}", data={"x.X_HW_Token": match.group(1)},
            headers={**headers, "Referer": f"{ont.url}{CT_RESET_PAGE}"},
        ) as resp:
            log.info("ont reboot accepted",
                     extra={"ont": ont.name, "kind": ont.kind,
                            "status": resp.status})
            return True
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
        log.info("ont connection dropped/hung at reset (expected)",
                 extra={"ont": ont.name, "kind": ont.kind})
        return True


async def _feng_handshake(session: aiohttp.ClientSession, ont: OntConfig,
                          referer: str | None = None,
                          cookie: dict[str, str] | None = None) -> dict[str, Any]:
    """One ``get_login_user`` round-trip; returns the parsed JSON."""
    from urllib.parse import quote

    tkn = quote(FENG_UA, safe="")
    headers = {"Referer": f"{ont.url}{referer or '/'}"} if referer else {}
    if cookie:
        headers.update(cookie)
    async with session.get(
        f"{ont.url}/cgi-bin/ajax?ajaxmethod=get_login_user&tkn={tkn}",
        headers=headers,
    ) as resp:
        if resp.status != 200:
            raise OntLoginError(f"get_login_user status {resp.status}")
        try:
            data = json.loads(await resp.text())
        except ValueError as exc:
            raise OntLoginError("get_login_user returned no JSON") from exc
    if data.get("sessionid") is None:
        raise OntLoginError("get_login_user issued no sessionid")
    return data


async def _trigger_reboot_fenghuo(ont: OntConfig, bind: str | None) -> bool:
    """FiberHome (Unicom) flow: ajax session + sha256 password + reboot call."""

    session = _session(bind, extra_headers={"User-Agent": FENG_UA})
    try:
        # 1+2) handshake & login (sha256 of the raw password, hex)
        hand = await _feng_handshake(session, ont)
        login_data = {
            "username": ont.username,
            "loginpd": hashlib.sha256(ont.password.encode()).hexdigest(),
            "port": "0",
            "sessionid": hand["sessionid"],
            "ajaxmethod": "do_login",
        }
        async with session.post(
            f"{ont.url}/cgi-bin/ajax", data=login_data,
            headers={"Referer": f"{ont.url}/"},
        ) as resp:
            body = await resp.text()
            set_cookie = resp.headers.get("Set-Cookie")
            token_pair = (set_cookie.split(";")[0].strip()) if set_cookie else None
            result = json.loads(body)
        if result.get("login_result") != 0:
            raise OntLoginError(
                f"login refused (login_result={result.get('login_result')})")
        auth = {"Cookie": token_pair} if token_pair else {}
        # 3) the reboot page re-handshakes, then posts ajaxmethod=reboot
        hand2 = await _feng_handshake(session, ont, cookie=auth)
        try:
            async with session.post(
                f"{ont.url}/cgi-bin/ajax",
                data={"sessionid": hand2["sessionid"],
                      "ajaxmethod": "reboot"},
                headers={"Referer": f"{ont.url}{FENG_REBOOT_PAGE}", **auth},
            ) as resp:
                result = json.loads(await resp.text())
            if result.get("success") == "true":
                log.info("ont reboot accepted",
                         extra={"ont": ont.name, "kind": ont.kind})
                return True
            raise OntLoginError(
                f"reboot not confirmed ({result.get('success')!r})")
        except (aiohttp.ClientError, asyncio.TimeoutError,
                OSError, ValueError):
            # the board may cut the connection / mangle the response
            # mid-reboot; that counts (live: ClientResponseError observed)
            log.info("ont connection dropped at reset (expected)",
                     extra={"ont": ont.name, "kind": ont.kind})
            return True
    finally:
        await session.close()


async def _wait_recovered(ont: OntConfig, bind: str | None,
                          timeout_s: float = RECOVER_TIMEOUT_S) -> bool:
    """Poll the ONT web root until it answers again."""

    deadline = time.monotonic() + timeout_s
    url = f"{ont.url}/"
    session = _session(bind)
    try:
        while time.monotonic() < deadline:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        log.info("ont web back", extra={"ont": ont.name})
                        return True
            except Exception:
                pass
            await asyncio.sleep(5)
    finally:
        await session.close()
    log.error("ont did not come back", extra={"ont": ont.name})
    return False
