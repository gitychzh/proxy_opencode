"""Settings loading and validation."""

from __future__ import annotations

import pytest

from proxy_opencode import config


def test_defaults_zen_direct_mode(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(
            ("UPSTREAM_", "OPENCODE_", "GATEWAY_", "REASONING_", "ZEN_", "HOST")
        ):
            monkeypatch.delenv(key, raising=False)
    s = config.load_settings()
    assert s.upstream_mode == "zen-direct"
    assert s.is_zen_direct
    assert s.zen_base_url == "https://opencode.ai/zen/v1"
    assert s.zen_api_key == ""
    assert s.dev_open  # no gateway keys
    assert s.host == "127.0.0.1"


def test_invalid_mode_rejected(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "bogus")
    with pytest.raises(ValueError):
        config.load_settings()


def test_serve_url_must_be_loopback(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "opencode-serve")
    monkeypatch.setenv("OPENCODE_SERVE_URL", "http://example.com:4096")
    with pytest.raises(ValueError):
        config.load_settings()


def test_passwordless_requires_127001(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "opencode-serve")
    monkeypatch.setenv("OPENCODE_SERVE_URL", "http://localhost:4096")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        config.load_settings()
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    s = config.load_settings()
    assert s.opencode_serve_models == list(config.DEFAULT_SERVE_MODELS)


def test_nonloopback_bind_requires_api_keys(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    with pytest.raises(ValueError, match="GATEWAY_API_KEYS"):
        config.load_settings()
    monkeypatch.setenv("GATEWAY_API_KEYS", "k1,k2")
    s = config.load_settings()
    assert s.host == "0.0.0.0"
    assert not s.dev_open


def test_loopback_bind_allows_dev_open(monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    s = config.load_settings()
    assert s.host == "127.0.0.1"
    assert s.dev_open


def test_public_models_accept_colon_in_display_name(monkeypatch):
    """Regression: an extra colon used to drop the entry silently, so the
    gateway quietly fell back to the built-in catalogue."""
    monkeypatch.setenv(
        "PUBLIC_MODELS", "dsv41f:DeepSeek V4.1 Flash:fast:opencode/big-pickle"
    )
    s = config.load_settings()
    assert s.public_models == [
        {
            "id": "dsv41f",
            "display_name": "DeepSeek V4.1 Flash:fast",
            "upstream": "opencode/big-pickle",
        }
    ]


def test_public_models_skips_malformed_entries(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODELS", "a:onlytwo,good:Good Name:up-model")
    s = config.load_settings()
    assert [m["id"] for m in s.public_models] == ["good"]


def test_zen_tools_mode_accepts_captured2(monkeypatch):
    for value in ("minimal", "captured2", "all"):
        monkeypatch.setenv("ZEN_TOOLS_MODE", value)
        assert config.load_settings().zen_tools_mode == value
    monkeypatch.setenv("ZEN_TOOLS_MODE", "bogus")
    with pytest.raises(ValueError):
        config.load_settings()


def test_upstream_trust_env_defaults_off(monkeypatch):
    """Ambient HTTP_PROXY must not steer the gateway by default: a stale
    proxy env var once 502'd a bucket whose network was fine."""
    monkeypatch.delenv("UPSTREAM_TRUST_ENV", raising=False)
    assert config.load_settings().upstream_trust_env is False
    monkeypatch.setenv("UPSTREAM_TRUST_ENV", "1")
    assert config.load_settings().upstream_trust_env is True
