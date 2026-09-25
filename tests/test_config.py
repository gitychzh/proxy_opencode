"""Settings loading and validation."""

from __future__ import annotations

import pytest

from proxy_opencode import config


def test_defaults_serve_mode(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("UPSTREAM_", "OPENCODE_", "GATEWAY_", "REASONING_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    s = config.load_settings()
    assert s.upstream_mode == "opencode-serve"
    assert s.is_opencode_serve
    assert s.dev_open  # no gateway keys


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
