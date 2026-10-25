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


def relay(status: int) -> dict[str, Any]:
    return {"__status__": status, "content": {"error": {"message": "x"}}}


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
