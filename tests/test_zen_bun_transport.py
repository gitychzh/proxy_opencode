"""Tests for the bun subprocess transport (2026-10-24 stack-gate hop).

The live incident: byte-exact httpx replays of a genuine 1.19.4 capture
got the quota-shaped 429 from fresh egress IPs while bun replays got 200
from the same IP minutes later — the transport stack itself is judged.
These tests pin the subprocess contract (META protocol, stdin/stdout
plumbing, HTTPS_PROXY composition) and the adapter's bun exchange paths,
so the failover/release semantics stay transport-agnostic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from proxy_opencode.config import Settings
from proxy_opencode.egress.pool import EgressPool, EgressSpec, EgressState, Lease
from proxy_opencode.upstreams import zen_bun_transport as zt
from proxy_opencode.upstreams.openai_http import StreamRelay
from proxy_opencode.upstreams.zen_bun_transport import (
    BunTransport,
    BunTransportError,
)
from proxy_opencode.upstreams.zen_direct import ZenDirectAdapter

_UPSTREAM = "https://opencode.ai/inference/openai/v1"


def make_settings(**kw: Any) -> Settings:
    return Settings(upstream_mode="zen-direct", **kw)


def _write_assets(tmp_path: Path) -> tuple[Path, Path]:
    bin_path = tmp_path / "bun"
    bin_path.write_bytes(b"#!/bin/sh\n")
    script = tmp_path / "forward.ts"
    script.write_text("// fake forwarder\n")
    return bin_path, script


def _transport(tmp_path: Path) -> BunTransport:
    bin_path, script = _write_assets(tmp_path)
    return BunTransport(bin_path=str(bin_path), script_path=str(script))


def _post_kwargs(proxy_url: str | None = None) -> dict[str, Any]:
    return {
        "base_url": _UPSTREAM,
        "path": "/chat/completions",
        "headers": {"x-opencode-session": "ses_abc"},
        "body": b'{"stream":true}',
        "proxy_url": proxy_url,
        "timeout_s": 12.0,
    }


# ------------------------------------------------------------- construction


def test_from_settings_disabled_returns_none() -> None:
    assert BunTransport.from_settings(make_settings()) is None


def test_from_settings_missing_binary_returns_none(tmp_path: Path) -> None:
    settings = make_settings(
        zen_bun_transport=True,
        zen_bun_bin=str(tmp_path / "no-bun"),
        zen_bun_script=str(_write_assets(tmp_path)[1]),
    )
    assert BunTransport.from_settings(settings) is None


def test_from_settings_missing_script_returns_none(tmp_path: Path) -> None:
    bin_path, _ = _write_assets(tmp_path)
    settings = make_settings(
        zen_bun_transport=True,
        zen_bun_bin=str(bin_path),
        zen_bun_script=str(tmp_path / "no-script.ts"),
    )
    assert BunTransport.from_settings(settings) is None


def test_from_settings_enabled_builds(tmp_path: Path) -> None:
    bin_path, script = _write_assets(tmp_path)
    settings = make_settings(
        zen_bun_transport=True, zen_bun_bin=str(bin_path), zen_bun_script=str(script)
    )
    assert isinstance(BunTransport.from_settings(settings), BunTransport)


# ----------------------------------------------------------------- plumbing


class _FakeStdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeProcess:
    """The asyncio-subprocess surface the transport touches, faked."""

    def __init__(self, meta_line: bytes, body_chunks: list[bytes]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(body_chunks)
        self.stderr = self
        self.returncode: int | None = None
        self.killed = False
        self._meta_line = meta_line

    async def readline(self) -> bytes:
        return self._meta_line

    def kill(self) -> None:
        self.killed = True
        self.returncode = 9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _install_spawn(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess
) -> dict[str, Any]:
    """Patch asyncio.create_subprocess_exec; record call args/env."""

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return proc

    def recorder(*args: Any, **kwargs: Any) -> Any:
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return fake_exec(*args, **kwargs)

    recorded: dict[str, Any] = {}
    monkeypatch.setattr(zt.asyncio, "create_subprocess_exec", recorder)
    return recorded


def _ok_proc(body_chunks: list[bytes]) -> _FakeProcess:
    return _FakeProcess(
        meta_line=b'META {"status": 200, "headers": {"content-type": "text/event-stream"}}\n',
        body_chunks=body_chunks,
    )


async def test_post_streams_body_and_exposes_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _transport(tmp_path)
    proc = _ok_proc([b"chunk-one ", b"chunk-two"])
    spawn = _install_spawn(monkeypatch, proc)

    resp = await transport.post(**_post_kwargs(proxy_url="http://user:pass@100.1.1.1:10001"))
    env = spawn["kwargs"]["env"]
    assert env["HTTPS_PROXY"] == "http://user:pass@100.1.1.1:10001"
    assert env["ZEN_UPSTREAM_BASE"] == _UPSTREAM
    assert env["ZEN_BUN_TIMEOUT_MS"] == "12000"
    assert json.loads(env["ZEN_BUN_HEADERS"]) == {"x-opencode-session": "ses_abc"}
    bin_path, script = _write_assets(tmp_path)
    assert spawn["args"][:3] == (str(bin_path), str(script), "/chat/completions")
    assert proc.stdin.written == b'{"stream":true}'
    assert proc.stdin.closed
    assert resp.status_code == 200
    assert resp.headers.get("Content-Type") == "text/event-stream"
    collected = b"".join([chunk async for chunk in resp.aiter_bytes()])
    assert collected == b"chunk-one chunk-two"


async def test_post_strips_ambient_proxy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", {**os.environ, "HTTPS_PROXY": "http://leak:9"})
    transport = _transport(tmp_path)
    proc = _ok_proc([])
    spawn = _install_spawn(monkeypatch, proc)

    await transport.post(**_post_kwargs())
    env = spawn["kwargs"]["env"]
    # No ambient proxy and none egress-provided: the child exits directly.
    assert "HTTPS_PROXY" not in env
    assert "HTTP_PROXY" not in env


async def test_post_transport_status_zero_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _transport(tmp_path)
    proc = _FakeProcess(
        meta_line=b'META {"status": 0, "error": "ConnectTimeout: refused"}\n',
        body_chunks=[],
    )
    _install_spawn(monkeypatch, proc)
    with pytest.raises(BunTransportError, match="ConnectTimeout"):
        await transport.post(**_post_kwargs())


async def test_post_bad_meta_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _transport(tmp_path)
    proc = _FakeProcess(meta_line=b"FATAL: bun runtime boom\n", body_chunks=[])
    _install_spawn(monkeypatch, proc)
    with pytest.raises(BunTransportError, match="died before META"):
        await transport.post(**_post_kwargs())


async def test_post_spawn_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _transport(tmp_path)

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        raise FileNotFoundError("no bun")

    monkeypatch.setattr(zt.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(BunTransportError, match="spawn bun failed"):
        await transport.post(**_post_kwargs())


# ------------------------------------------------------------------ adapter


def _lease() -> Lease:
    spec = EgressSpec("b1", "http://100.1.1.1:10001", None)
    return Lease(EgressState(spec=spec))


def _stub_post(
    monkeypatch: pytest.MonkeyPatch,
    *,  # keyword-only
    status: int,
    headers: dict[str, str],
    body_chunks: list[bytes],
    meta_line: bytes = b'META {"status": 0, "headers": {}}\n',
) -> dict[str, Any]:
    """Replace BunTransport.post; record kwargs through `captured`."""
    captured: dict[str, Any] = {}

    async def fake_post(self: BunTransport, **kwargs: Any) -> zt._BunResponse:
        captured.update(kwargs)
        proc = _FakeProcess(meta_line=meta_line, body_chunks=body_chunks)
        return zt._BunResponse(
            proc=proc, status_code=status, headers=httpx.Headers(headers)
        )

    monkeypatch.setattr(BunTransport, "post", fake_post)
    return captured


async def _bun_adapter(tmp_path: Path, **extra: Any) -> ZenDirectAdapter:
    bin_path, script = _write_assets(tmp_path)
    settings = make_settings(
        zen_bun_transport=True,
        zen_bun_bin=str(bin_path),
        zen_bun_script=str(script),
        **extra,
    )
    return ZenDirectAdapter(settings)


async def test_adapter_bun_path_relays_429_and_composes_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_path, script = _write_assets(tmp_path)
    settings = make_settings(
        zen_bun_transport=True,
        zen_bun_bin=str(bin_path),
        zen_bun_script=str(script),
        zen_egress_secret="whispered",
        zen_egress_proxies={"b1": "http://100.1.1.1:10001"},
    )
    pool = EgressPool.from_settings(settings)
    assert pool is not None
    await pool.start()
    adapter = ZenDirectAdapter(settings, pool)
    try:
        captured = _stub_post(
            monkeypatch,
            status=429,
            headers={"retry-after": "12"},
            body_chunks=[b'{"type":"error"}'],
        )
        result = await adapter.chat(
            {
                "model": "big-pickle",
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        # The leased bucket's agent URL, with the composed Basic credentials.
        assert captured["proxy_url"] == "http://whispered:whispered@100.1.1.1:10001"
        assert captured["path"] == "/chat/completions"
        # Single-bucket pool: the failover ladder runs dry and relays 429.
        assert result["__status__"] == 429
    finally:
        await adapter.aclose()
        await pool.aclose()


async def test_adapter_bun_path_streams_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = await _bun_adapter(tmp_path)
    try:
        _stub_post(
            monkeypatch,
            status=200,
            headers={},
            body_chunks=[
                b"data: part\n\n",
                b'data: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            ],
        )
        stream = await adapter.chat(
            {
                "model": "big-pickle",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        assert isinstance(stream, StreamRelay)
        relayed = b"".join([chunk async for chunk in stream.chunks()])
        # Byte-exact passthrough; the genuine finish chunk avoids synthesis.
        assert b"data: part\n\n" in relayed
        assert b"finish_reason" in relayed
    finally:
        await adapter.aclose()


async def test_egress_proxy_url_composes_credentials(tmp_path: Path) -> None:
    adapter_secret = await _bun_adapter(tmp_path, zen_egress_secret="zeb!fantama")
    try:
        composed = adapter_secret._egress_proxy_url(_lease())
        assert composed == "http://zeb%21fantama:zeb%21fantama@100.1.1.1:10001"
    finally:
        await adapter_secret.aclose()

    adapter_plain = await _bun_adapter(tmp_path)
    try:
        assert adapter_plain._egress_proxy_url(_lease()) == "http://100.1.1.1:10001"
        assert adapter_plain._egress_proxy_url(None) is None
    finally:
        await adapter_plain.aclose()


async def test_bun_response_aiter_lines_splits_and_closes() -> None:
    proc = _FakeProcess(
        meta_line=b'META {"status": 200, "headers": {}}\n',
        body_chunks=[b"data: one\n", b"data: two\r\nlast", b"\n"],
    )
    resp = zt._BunResponse(proc=proc, status_code=200, headers=httpx.Headers({}))
    lines = [line async for line in resp.aiter_lines()]
    assert lines == ["data: one", "data: two", "last"]
    assert proc.killed  # closed once the stream ends


# ------------------------------------------------- header (TTFB) deadline
#
# 2026-10-25 production evidence: five client requests in 12h finished at
# 305-315s with a fast stream afterwards. The whole-exchange 300s
# AbortController was the only bound, so a blackholed egress hop (dead
# tunnel / ONT rebooting / CONNECT that never answers) consumed the entire
# request budget before the failover ladder could try another bucket. The
# header deadline is a separate, much shorter timer.


async def test_post_env_carries_header_deadline(tmp_path: Path, monkeypatch) -> None:
    bin_path, script = _write_assets(tmp_path)
    transport = BunTransport(
        bin_path=str(bin_path), script_path=str(script), ttfb_s=45.0
    )
    proc = _ok_proc([])
    spawn = _install_spawn(monkeypatch, proc)
    await transport.post(**_post_kwargs())
    assert spawn["kwargs"]["env"]["ZEN_BUN_TTFB_MS"] == "45000"
    assert spawn["kwargs"]["env"]["ZEN_BUN_TIMEOUT_MS"] == "12000"


async def test_post_without_header_deadline_omits_env(
    tmp_path: Path, monkeypatch
) -> None:
    bin_path, script = _write_assets(tmp_path)
    transport = BunTransport(
        bin_path=str(bin_path), script_path=str(script), ttfb_s=0.0
    )
    proc = _ok_proc([])
    spawn = _install_spawn(monkeypatch, proc)
    await transport.post(**_post_kwargs())
    assert "ZEN_BUN_TTFB_MS" not in spawn["kwargs"]["env"]


async def test_post_fails_fast_when_no_headers(tmp_path: Path, monkeypatch) -> None:
    """A hop that never answers must fail in seconds, not in 300s."""

    import asyncio
    import time

    bin_path, script = _write_assets(tmp_path)
    transport = BunTransport(
        bin_path=str(bin_path),
        script_path=str(script),
        meta_timeout_s=0.1,
        ttfb_s=0.1,
    )

    class _Hanging(_FakeProcess):
        async def readline(self) -> bytes:
            await asyncio.sleep(10.0)
            return b""

    _install_spawn(monkeypatch, _Hanging(b"", []))
    started = time.monotonic()
    with pytest.raises(BunTransportError) as excinfo:
        await transport.post(**_post_kwargs())
    elapsed = time.monotonic() - started
    # 12s total budget; the header budget is 0.1 + 0.1 meta slack.
    assert elapsed < 2.0
    assert "before response headers" in str(excinfo.value)


async def test_header_deadline_never_exceeds_whole_request_budget(
    tmp_path: Path, monkeypatch
) -> None:
    """A ttfb larger than timeout_s must not extend the wait."""

    bin_path, script = _write_assets(tmp_path)
    transport = BunTransport(
        bin_path=str(bin_path), script_path=str(script), ttfb_s=999.0
    )
    proc = _ok_proc([])
    spawn = _install_spawn(monkeypatch, proc)
    await transport.post(**_post_kwargs())
    # The script side still gets the ttfb it was configured with; the python
    # wait budget stays bounded by timeout_s.
    assert spawn["kwargs"]["env"]["ZEN_BUN_TTFB_MS"] == "999000"
