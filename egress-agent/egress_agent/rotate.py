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


def _session(bind_source: str | None) -> aiohttp.ClientSession:
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
