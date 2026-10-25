"""bun-based upstream transport for the zen-direct adapter.

2026-10-24 incident root cause: the anonymous free tier began judging the
CLIENT TRANSPORT STACK. An httpx request (python OpenSSL, HTTP/1.1)
replaying a byte-exact genuine 1.19.4 client capture got HTTP 429
``FreeUsageLimitError`` from a FRESH residential egress IP, while bun
replays (boringssl, HTTP/2) of the same bytes got 200 from the very same
IP within minutes — repeated on such freshly rotated IPs no matter what
header/body mutations passed; every single httpx-originated attempt in
the pool 429'd. The transport-stack verdict therefore rides on top of
(and masks itself as) the per-IP quota gate.

Mitigation: hand the one TLS + HTTP hop to bun — the runtime the official
client embeds — via one short-lived ``bun forward.ts`` subprocess per
upstream attempt (see ``proxy_opencode/zen_bun/forward.ts`` for the
subprocess contract). The caller keeps full ownership of headers, body,
status classification and release/cordon semantics, so the layer-1 shape
gate, failover ladder and egress pool all behave exactly as before; only
the wire transport changes.

The subprocess is pure transport: it holds no credentials of its own
(the caller passes the composed headers), never opens local listening
ports, and streams the SSE body verbatim on stdout. When the bun binary
is absent the adapter silently falls back to the httpx path (and tests
exercise both).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("proxy_opencode.zen_bun")

_META_PREFIX = "META "
_META_PREFIX_BYTES = _META_PREFIX.encode()
# Location of the bundled forward script when the package tree is intact
# (repo checkout and the ECS image both). Overridable via
# ``zen_bun_script``/``ZEN_BUN_SCRIPT`` for packaged installs.
_DEFAULT_SCRIPT = Path(__file__).parent.parent / "zen_bun" / "forward.ts"

# Ambient proxy environment must never leak into the child: the exit route
# is chosen explicitly per attempt (the leased bucket's agent) or absent.
_AMBIENT_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class BunTransportError(Exception):
    """Spawn/protocol/timeout failure of the bun forwarder subprocess."""


class BunTransport:
    """Drives ``bun forward.ts`` subprocesses for upstream exchanges.

    One subprocess per attempt (spawn cost ~tens of ms against requests
    measured in seconds), so there is no daemon to operate, no local
    listening port and no shared state to babysit: the egress pool's
    lease/proxy semantics apply unchanged.
    """

    def __init__(
        self,
        *,
        bin_path: str,
        script_path: str,
        meta_timeout_s: float = 30.0,
        ttfb_s: float = 60.0,
    ) -> None:
        self._bin = bin_path
        self._script = script_path
        # Deadline for the first stderr META line (fetch resolved or
        # aborted). 300s request timeouts are bounded inside the script
        # via its AbortController; this only guards spawn/protocol hangs.
        self._meta_timeout = max(1.0, float(meta_timeout_s))
        # Response-header deadline. The script enforces the same bound with
        # its own timer; this second layer covers a child that hangs before
        # the timer can fire (spawn stall, unread stderr). Keeping both is
        # deliberate: the python guard is the one the adapter's failover
        # ladder can rely on regardless of the script version on disk.
        self._ttfb_s = max(0.0, float(ttfb_s))

    @classmethod
    def from_settings(cls, settings: object) -> BunTransport | None:
        """Build from Settings; None when disabled or assets are missing."""

        if not bool(getattr(settings, "zen_bun_transport", False)):
            return None
        bin_path = str(getattr(settings, "zen_bun_bin", "") or "").strip()
        script_path = (
            str(getattr(settings, "zen_bun_script", "") or "").strip()
            or str(_DEFAULT_SCRIPT)
        )
        if not bin_path or not Path(bin_path).exists():
            logger.warning(
                "bun transport disabled: binary not found",
                extra={"zen_bun_bin": bin_path},
            )
            return None
        if not Path(script_path).exists():
            logger.warning(
                "bun transport disabled: forward script not found",
                extra={"zen_bun_script": script_path},
            )
            return None
        return cls(
            bin_path=bin_path,
            script_path=script_path,
            ttfb_s=float(getattr(settings, "zen_ttfb_s", 60.0) or 0.0),
        )

    async def post(
        self,
        *,
        base_url: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        proxy_url: str | None,
        timeout_s: float,
    ) -> "_BunResponse":
        """Run one upstream exchange through the bun forwarder.

        ``proxy_url`` is the leased bucket's agent HTTP-proxy (with
        credentials pre-composed); ``None`` means direct egress. Returns a
        :class:`_BunResponse` carrying status/headers and streaming body;
        non-200 statuses are returned too (the caller relays/failovers
        exactly as with httpx). Transport failures raise
        :class:`BunTransportError`.
        """
        env = dict(os.environ)
        for name in _AMBIENT_PROXY_VARS:
            env.pop(name, None)
        if proxy_url:
            env["HTTPS_PROXY"] = proxy_url
        # Hop observability for the transport-fingerprint gate: log WHERE
        # the attempt egresses without ever logging the credential-bearing
        # URL (secret red-lines). Wrong/missing lease proxy would show as
        # proxy=DIRECT while the pool believes it leased a bucket.
        proxy_hop = "DIRECT"
        if proxy_url:
            parsed = urlsplit(proxy_url)
            proxy_hop = f"{parsed.hostname}:{parsed.port}"
        logger.info(
            "bun upstream attempt: proxy=%s timeout_ms=%d",
            proxy_hop,
            int(timeout_s * 1000),
        )
        env["ZEN_UPSTREAM_BASE"] = base_url
        env["ZEN_BUN_HEADERS"] = json.dumps(dict(headers))
        env["ZEN_BUN_TIMEOUT_MS"] = str(int(timeout_s * 1000))
        if self._ttfb_s > 0:
            env["ZEN_BUN_TTFB_MS"] = str(int(self._ttfb_s * 1000))
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                self._script,
                path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise BunTransportError(f"spawn bun failed: {exc}") from exc

        # Wait for META under the header deadline, not the whole-request one:
        # a hop that never answers must cost one failover step, not the
        # client's entire patience budget.
        budget = timeout_s
        if 0 < self._ttfb_s < budget:
            budget = self._ttfb_s
        deadline = budget + self._meta_timeout
        try:
            if proc.stdin is not None:
                proc.stdin.write(body)
                await proc.stdin.drain()
                proc.stdin.close()
            if proc.stderr is None:  # pragma: no cover - PIPE is forced
                raise BunTransportError("bun forwarder has no stderr")
            meta_line = await asyncio.wait_for(
                proc.stderr.readline(),
                timeout=deadline,
            )
        except (TimeoutError, OSError) as exc:
            await self._terminate(proc)
            raise BunTransportError(
                f"bun forwarder stuck before response headers "
                f"({type(exc).__name__}, budget {deadline:.0f}s)"
            ) from exc
        if not meta_line.startswith(_META_PREFIX_BYTES):
            await self._terminate(proc)
            raise BunTransportError("bun forwarder died before META")
        meta = _parse_meta(meta_line)
        if meta["status"] == 0:
            await self._terminate(proc)
            raise BunTransportError(str(meta.get("error") or "bun fetch failed"))
        return _BunResponse(
            proc=proc,
            status_code=int(meta["status"]),
            headers=httpx.Headers(meta.get("headers") or {}),
        )

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:  # pragma: no cover - reap is best-effort
            pass


def _parse_meta(line: bytes) -> dict[str, Any]:
    try:
        meta = json.loads(line[len(_META_PREFIX) :].decode("utf-8", "replace"))
    except (ValueError, UnicodeError) as exc:
        raise BunTransportError("bun META line unparsable") from exc
    if not isinstance(meta, dict) or not isinstance(meta.get("status"), int):
        raise BunTransportError("bun META line malformed")
    return meta


class _BunResponse:
    """httpx.Response-shaped facade over the bun forwarder process.

    Implements exactly the surface the adapter consumes —
    ``status_code``/``headers``/``aiter_bytes()``/``aiter_lines()``/
    ``aclose()`` — so the SSE aggregation and :class:`StreamRelay` stay
    transport-agnostic (httpx responses satisfy the same duck surface).
    Any unconsumed stdout is discarded on close so the child can exit.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        status_code: int,
        headers: httpx.Headers,
    ) -> None:
        self._proc = proc
        self.status_code = status_code
        self.headers = headers

    async def _iterate_stdout(self) -> AsyncIterator[bytes]:
        try:
            while True:
                if self._proc.stdout is None:
                    return
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    return
                yield chunk
        finally:
            await self.aclose()

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        """Stream the upstream body verbatim (SSE passthrough)."""
        return self._iterate_stdout()

    async def aiter_lines(self) -> AsyncIterator[str]:
        """Line-split the stream like ``httpx.Response.aiter_lines``.

        Mirrors httpx's semantics: ``\\n`` separates lines, the newline is
        stripped, a trailing ``\\r`` (CRLF) is dropped, and any residual
        unterminated tail is yielded at EOF.
        """
        buf = b""
        async for chunk in self._iterate_stdout():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.endswith(b"\r"):
                    line = line[:-1]
                yield line.decode("utf-8", "replace")
        if buf:
            if buf.endswith(b"\r"):
                buf = buf[:-1]
            yield buf.decode("utf-8", "replace")

    async def aclose(self) -> None:
        """Terminate the child (stream end, abort or caller abandonment)."""
        proc = self._proc
        if proc.returncode is None:
            proc.kill()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    await stream.read()
                except Exception:  # pragma: no cover - best-effort drain
                    pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:  # pragma: no cover - reap is best-effort
            pass
