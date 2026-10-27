"""Tests for the backup-chain failover wrapper (zen-direct -> proxy_ms)."""

from __future__ import annotations

from typing import Any

import pytest

from proxy_opencode.config import Settings
from proxy_opencode.upstreams.failover import FailoverAdapter


class FakeAdapter:
    """Scriptable primary: raises or returns queued results in order."""

    name = "zen-direct"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcomes: list[Any] = []

    async def chat(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def list_models(self) -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "primary"}]}


class FakeSecondary(FakeAdapter):
    name = "openai"


def make_settings(**kw: Any) -> Settings:
    return Settings(
        zen_fallback_base_url="http://127.0.0.1:10003/v1",
        zen_fallback_api_key="test-key",
        **kw,
    )


def make_adapter(
    primary: FakeAdapter, **kw: Any
) -> tuple[FailoverAdapter, FakeSecondary]:
    secondary = FakeSecondary()
    adapter = FailoverAdapter(primary, make_settings(**kw), secondary=secondary)
    return adapter, secondary


def relay(status: int, content: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "__status__": status,
        "content": content or {"error": {"message": "x"}},
    }


async def test_transport_failure_engages_backup() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    secondary.outcomes = [{"id": "ok", "model": "ms-model"}]
    primary.outcomes = [ConnectionError("tunnel dead")]

    result = await adapter.chat({"model": "m1", "messages": []})

    assert result == {"id": "ok", "model": "ms-model"}
    assert len(primary.calls) == 1 and len(secondary.calls) == 1
    assert adapter.fallback_status()["engaged"] is True


async def test_quota_429_and_5xx_and_503_gate_engage_backup() -> None:
    for status in (429, 500, 502, 503, 504):
        primary = FakeAdapter()
        adapter, secondary = make_adapter(primary)
        secondary.outcomes = [{"id": "ok"}]
        primary.outcomes = [relay(status)]
        result = await adapter.chat({"model": "m1"})
        assert result == {"id": "ok"}
        assert adapter.fallback_status()["engaged"] is True


async def test_request_level_4xx_relays_untouched() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    primary.outcomes = [relay(404)]

    result = await adapter.chat({"model": "nope"})

    assert result == relay(404)
    assert secondary.calls == []  # backup never tried
    assert adapter.fallback_status()["engaged"] is False


async def test_hold_window_skips_primary_then_half_open_recovers() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary, zen_fallback_hold_s=3600.0)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms-1"}]
    await adapter.chat({"model": "m"})

    # Breaker open: primary is NOT called during the hold window.
    secondary.outcomes = [{"id": "via-ms-2"}]
    result = await adapter.chat({"model": "m"})
    assert result == {"id": "via-ms-2"}
    assert len(primary.calls) == 1

    # Shrink the window: the next request probes the primary (half-open);
    # a primary success closes the breaker without engaging the backup.
    adapter._fallback_until = 0.0
    primary.outcomes = [{"id": "primary-ok"}]
    result = await adapter.chat({"model": "m"})
    assert result == {"id": "primary-ok"}
    assert len(primary.calls) == 2
    assert adapter.fallback_status()["engaged"] is False


async def test_half_open_probe_failure_re_arms_breaker() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary, zen_fallback_hold_s=3600.0)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms"}]
    await adapter.chat({"model": "m"})
    assert len(primary.calls) == 1

    # Half-open probe: primary fails again -> breaker re-arms, backup serves.
    adapter._fallback_until = 0.0
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms-2"}]
    result = await adapter.chat({"model": "m"})
    assert result == {"id": "via-ms-2"}
    assert len(primary.calls) == 2
    assert adapter.fallback_status()["engaged"] is True


async def test_model_map_rewrites_for_secondary_only() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(
        primary,
        zen_fallback_model_map={
            "deepseek-ai/deepseek-v4.1-flash":
                "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        },
    )
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "ok"}]

    await adapter.chat({"model": "deepseek-ai/deepseek-v4.1-flash"})

    assert secondary.calls[0]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"


async def test_unmapped_model_id_passes_through() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "ok"}]

    await adapter.chat({"model": "other/model"})

    assert secondary.calls[0]["model"] == "other/model"


async def test_default_model_catches_unmapped_ids() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(
        primary,
        zen_fallback_default_model="deepseek-ai/DeepSeek-V4.1-Flash-1017",
    )
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "ok"}]

    # "big-pickle" is the route layer's resolved zen id: no map entry, so the
    # catch-all must be used — the backup gateway has no such model.
    await adapter.chat({"model": "big-pickle"})

    assert secondary.calls[0]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"


async def test_backup_error_relay_releases_hold_for_next_primary_probe() -> None:
    """A backup that answers with an error must not hold traffic for 120s.

    Measured 2026-10-25: the primary chain returned a (false) quota 429, the
    breaker parked traffic on proxy_ms, and proxy_ms answered its own billing
    429 "insufficient balance" — which the client reports as a non-retryable
    billing failure. When the backup cannot serve, the next request must
    probe the primary again (it may already be healthy) instead of being
    parked on a dead backup for the whole hold window.
    """

    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary, zen_fallback_hold_s=3600.0)
    primary.outcomes = [relay(429), {"id": "ok-after-recovery"}]
    secondary.outcomes = [relay(429), {"id": "ok-via-ms"}]

    first = await adapter.chat({"model": "m"})
    # Backup billing 429 is reshaped into a gateway-owned retryable 503:
    # the client must never see the backup's billing error wearing the
    # primary chain's identity.
    assert first["__status__"] == 503
    assert first["content"]["error"]["code"] == "fallback_backup_error"
    assert "backup gateway" in first["content"]["error"]["message"]
    assert adapter.fallback_status()["engaged"] is False  # hold released
    # Next request goes back to the primary instead of the dead backup.
    second = await adapter.chat({"model": "m"})
    assert second["id"] == "ok-after-recovery"
    assert len(primary.calls) == 2 and len(secondary.calls) == 1


async def test_backup_error_body_is_not_echoed_to_client() -> None:
    """The backup's own error body must not leak through the relay.

    Measured 2026-10-27 00:03Z: the client received ModelScope's billing
    text ("HTTP 429: insufficient balance") attributed to the zen chain,
    which read as a non-retryable billing wall and killed retries. The
    reshaped answer names the backup chain and status instead.
    """

    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [
        relay(
            429,
            {
                "error": {
                    "message": "HTTP 429: insufficient balance",
                    "code": "insufficient_balance",
                }
            },
        )
    ]

    result = await adapter.chat({"model": "m"})
    assert result["__status__"] == 503  # retryable, gateway-owned shape
    body = result["content"]
    assert body["error"]["code"] == "fallback_backup_error"
    assert "insufficient balance" not in body["error"]["message"]
    assert "HTTP 429" in body["error"]["message"]  # attribution, not echo


async def test_backup_unreachable_raises_loudly() -> None:
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [ConnectionError("proxy_ms down")]

    with pytest.raises(ConnectionError, match="both unreachable"):
        await adapter.chat({"model": "m"})
    # Breaker stays armed for the hold window regardless.
    assert adapter.fallback_status()["engaged"] is True


async def test_list_models_degrades_to_secondary() -> None:
    class DeadPrimary(FakeAdapter):
        async def list_models(self) -> dict[str, Any]:
            raise ConnectionError("dead")

    primary = DeadPrimary()
    adapter, secondary = make_adapter(primary)
    result = await adapter.list_models()
    assert isinstance(result, dict)
    assert result["data"] == [{"id": "primary"}]  # served by the fake secondary


async def test_disabled_feature_returns_bare_primary() -> None:
    from proxy_opencode.upstreams import build_adapter

    settings = Settings(upstream_mode="openai")
    adapter = build_adapter(settings)
    assert not hasattr(adapter, "fallback_status")


async def test_build_adapter_wraps_when_configured() -> None:
    from proxy_opencode.upstreams import build_adapter

    settings = Settings(
        upstream_mode="openai",
        zen_fallback_base_url="http://127.0.0.1:10003/v1",
    )
    adapter = build_adapter(settings)
    assert hasattr(adapter, "fallback_status")
    assert adapter.name == "openai"


# ------------------------------------------------ recovery hysteresis
#
# 2026-10-25 production evidence: a primary that served roughly every other
# request produced engaged/recovered churn at request rate (57 engaged and
# 11 recovered events in a day, six round trips in 20 minutes). Every cycle
# switched the model served to the client, because the backup speaks a
# different id.


async def test_recovery_needs_consecutive_successes(caplog) -> None:  # type: ignore[no-untyped-def]
    import time

    primary = FakeAdapter()
    adapter, secondary = make_adapter(
        primary, zen_fallback_hold_s=3600.0, zen_fallback_recover_after=2
    )
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms"}]
    await adapter.chat({"model": "m"})

    # Hold window over: the next requests are half-open probes.
    adapter._fallback_until = time.monotonic() - 1.0
    with caplog.at_level("WARNING", logger="proxy_opencode.failover"):
        primary.outcomes = [{"id": "p1"}]
        assert await adapter.chat({"model": "m"}) == {"id": "p1"}
        assert "FALLBACK RECOVERED" not in caplog.text

        primary.outcomes = [{"id": "p2"}]
        assert await adapter.chat({"model": "m"}) == {"id": "p2"}
        assert caplog.text.count("FALLBACK RECOVERED") == 1
        assert "PRODUCTION INCIDENT CLOSED" in caplog.text


async def test_failure_resets_the_recovery_streak() -> None:
    import time

    primary = FakeAdapter()
    adapter, secondary = make_adapter(
        primary, zen_fallback_hold_s=3600.0, zen_fallback_recover_after=2
    )
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms"}]
    await adapter.chat({"model": "m"})

    adapter._fallback_until = time.monotonic() - 1.0
    primary.outcomes = [{"id": "p1"}]
    await adapter.chat({"model": "m"})
    # One failure between two successes re-arms and zeroes the streak.
    primary.outcomes = [relay(503)]
    secondary.outcomes = [{"id": "via-ms-2"}]
    assert await adapter.chat({"model": "m"}) == {"id": "via-ms-2"}
    assert adapter.fallback_status()["engaged"] is True


async def test_re_trip_is_logged_as_continuation(caplog) -> None:  # type: ignore[no-untyped-def]
    """The second trip of one outage must not read like a new incident."""

    import time

    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary, zen_fallback_hold_s=3600.0)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms"}]
    await adapter.chat({"model": "m"})

    adapter._fallback_until = time.monotonic() - 1.0
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "via-ms-2"}]
    caplog.clear()
    with caplog.at_level("INFO", logger="proxy_opencode.failover"):
        await adapter.chat({"model": "m"})
    assert caplog.text.count("FALLBACK ENGAGED") == 0
    assert caplog.text.count("FALLBACK RE-ENGAGED") == 1


# ------------------------------------------------ timeout / gate triggers
# Owner requirement (2026-10-26): ANY failure OR timeout on the primary must
# slide to GLM-5.3-Flash on the ModelScope gateway (proxy_ms), visibly.


async def test_gate_403_and_upstream_408_engage_backup() -> None:
    """Free-tier shape gate (403) and upstream timeout (408) both fail over."""

    for status in (403, 408):
        primary = FakeAdapter()
        adapter, secondary = make_adapter(primary)
        secondary.outcomes = [{"id": "ok"}]
        primary.outcomes = [relay(status)]
        assert await adapter.chat({"model": "m1"}) == {"id": "ok"}
        assert adapter.fallback_status()["engaged"] is True


async def test_client_fault_401_422_never_switches() -> None:
    for status in (401, 422):
        primary = FakeAdapter()
        adapter, secondary = make_adapter(primary)
        primary.outcomes = [relay(status)]
        assert await adapter.chat({"model": "m1"}) == relay(status)
        assert secondary.calls == []


async def test_primary_deadline_fails_over() -> None:
    """A primary that never answers within the deadline is a timeout, not a hang."""

    import asyncio

    class HangingPrimary(FakeAdapter):
        async def chat(self, payload: dict[str, Any]) -> Any:
            await asyncio.sleep(30)
            return {"id": "never"}

    primary = HangingPrimary()
    adapter, secondary = make_adapter(primary, zen_fallback_primary_timeout_s=0.05)
    secondary.outcomes = [{"id": "via-ms"}]

    result = await adapter.chat({"model": "m"})

    assert result == {"id": "via-ms"}
    assert adapter.fallback_status()["engaged"] is True


async def test_stalled_stream_prefetch_fails_over() -> None:
    """A stream with headers but no first byte must fail over before any output."""

    import asyncio

    from proxy_opencode.upstreams.openai_http import StreamRelay

    class SilentResponse:
        status_code = 200
        headers = {}

        async def aiter_bytes(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            yield b"data: {}\n\n"

        async def aiter_lines(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            yield ""

        async def aclose(self) -> None:
            return None

    class StreamingPrimary(FakeAdapter):
        async def chat(self, payload: dict[str, Any]) -> Any:
            self.calls.append(payload)
            return StreamRelay(SilentResponse(), {})  # type: ignore[arg-type]

    primary = StreamingPrimary()
    adapter, secondary = make_adapter(
        primary, zen_fallback_first_chunk_timeout_s=0.05
    )
    secondary.outcomes = [{"id": "via-ms"}]

    result = await adapter.chat({"model": "m", "stream": True})

    assert result == {"id": "via-ms"}
    assert adapter.fallback_status()["engaged"] is True


async def test_prefetched_stream_replays_first_chunk_once() -> None:
    """A healthy stream: the prefetched first chunk is handed over intact."""

    from proxy_opencode.upstreams.openai_http import StreamRelay

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        async def aiter_bytes(self):  # type: ignore[no-untyped-def]
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aiter_lines(self):  # type: ignore[no-untyped-def]
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'

        async def aclose(self) -> None:
            return None

    class StreamingPrimary(FakeAdapter):
        async def chat(self, payload: dict[str, Any]) -> Any:
            self.calls.append(payload)
            return StreamRelay(Response(), {})  # type: ignore[arg-type]

    primary = StreamingPrimary()
    adapter, secondary = make_adapter(primary, zen_fallback_first_chunk_timeout_s=5)
    result = await adapter.chat({"model": "m", "stream": True})

    assert isinstance(result, StreamRelay)
    body = b"".join([chunk async for chunk in result.chunks()])
    assert body.count(b'"content":"hi"') == 1
    assert b"[DONE]" in body
    assert secondary.calls == []


async def test_backup_is_announced_to_the_client() -> None:
    """Headers + SSE comment must name the backup and the model it served."""

    from proxy_opencode.upstreams.failover import (
        FALLBACK_HEADER,
        FALLBACK_MODEL_HEADER,
        fallback_headers,
        fallback_note,
    )

    primary = FakeAdapter()
    adapter, secondary = make_adapter(
        primary, zen_fallback_default_model="ZhipuAI/GLM-5.3-Flash"
    )
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "ok"}]

    await adapter.chat({"model": "big-pickle"})

    headers = fallback_headers()
    assert headers[FALLBACK_HEADER] == "proxy_ms"
    assert headers[FALLBACK_MODEL_HEADER] == "ZhipuAI/GLM-5.3-Flash"
    note = fallback_note()
    assert note.startswith(": ")
    assert "model=ZhipuAI/GLM-5.3-Flash" in note
    assert note.endswith("\n\n")


async def test_primary_answer_leaves_no_marker() -> None:
    from proxy_opencode.upstreams.failover import fallback_headers, fallback_note

    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    primary.outcomes = [{"id": "primary-ok"}]

    await adapter.chat({"model": "m"})

    assert fallback_headers() == {}
    assert fallback_note() == ""


# ---------------------------------------------- production-incident logging
# Owner requirement (2026-10-27): a fallback engagement is a production
# incident and must be impossible to miss — ERROR level (survives
# INFO-filtered collection), a greppable "PRODUCTION INCIDENT" prefix, and a
# severity field on the JSONL event; recovery closes the incident.


async def test_first_trip_is_error_level_production_incident(caplog) -> None:  # type: ignore[no-untyped-def]
    primary = FakeAdapter()
    adapter, secondary = make_adapter(primary)
    primary.outcomes = [relay(429)]
    secondary.outcomes = [{"id": "ok"}]

    with caplog.at_level("INFO", logger="proxy_opencode.failover"):
        await adapter.chat({"model": "m"})

    error_lines = [
        r for r in caplog.records
        if r.name == "proxy_opencode.failover" and r.levelname == "ERROR"
    ]
    assert any("PRODUCTION INCIDENT - FALLBACK ENGAGED" in r.getMessage()
               for r in error_lines)
    assert any("proxy_ms" in r.getMessage() for r in error_lines)


async def test_incident_severity_lands_on_jsonl_events(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    from proxy_opencode.egress.eventlog import EVENTS

    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    try:
        primary = FakeAdapter()
        adapter, secondary = make_adapter(primary)
        primary.outcomes = [relay(429)]
        secondary.outcomes = [{"id": "ok"}]
        await adapter.chat({"model": "m"})

        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        engaged = [e for e in events if e["event"] == "fallback_engaged"]
        assert len(engaged) == 1
        assert engaged[0]["severity"] == "incident"
        assert engaged[0]["secondary"]
        # "m" maps to the default model; with no default configured the
        # unmapped id would pass through unchanged — both are valid shapes,
        # so only assert the field exists.
        assert "model" in engaged[0]
    finally:
        EVENTS.configure("")


async def test_backup_failure_is_marked_as_incident(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    import pytest

    from proxy_opencode.egress.eventlog import EVENTS

    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    try:
        primary = FakeAdapter()
        adapter, secondary = make_adapter(primary)
        primary.outcomes = [relay(429)]
        secondary.outcomes = [ConnectionError("proxy_ms down")]
        with pytest.raises(ConnectionError, match="both unreachable"):
            await adapter.chat({"model": "m"})

        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failed = [e for e in events if e["event"] == "fallback_failed"]
        assert len(failed) == 1
        assert failed[0]["severity"] == "incident"
    finally:
        EVENTS.configure("")


async def test_re_trip_keeps_continuation_severity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A re-trip is the same incident continuing, not a new one."""

    import json
    import time

    from proxy_opencode.egress.eventlog import EVENTS

    path = tmp_path / "events.jsonl"
    EVENTS.configure(str(path))
    try:
        primary = FakeAdapter()
        adapter, secondary = make_adapter(primary, zen_fallback_hold_s=3600.0)
        primary.outcomes = [relay(429)]
        secondary.outcomes = [{"id": "via-ms"}]
        await adapter.chat({"model": "m"})

        adapter._fallback_until = time.monotonic() - 1.0
        primary.outcomes = [relay(429)]
        secondary.outcomes = [{"id": "via-ms-2"}]
        await adapter.chat({"model": "m"})

        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        engaged = [e for e in events if e["event"] == "fallback_engaged"]
        assert [e["severity"] for e in engaged] == ["incident", "continuation"]
    finally:
        EVENTS.configure("")
