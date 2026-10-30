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
    # round 5 defaults: genuine-prompt marker + free-id rewrite map empty
    assert s.zen_marker_mode == "full"
    assert s.zen_model_map == {}
    # 2026-10-25: hard-429 rotations cannot storm the ONT (30 min floor)
    assert s.zen_egress_rotate_min_interval_s == 1800.0


def test_egress_rotate_min_interval_env(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.setenv("ZEN_EGRESS_ROTATE_MIN_INTERVAL_S", "0")
    assert config.load_settings().zen_egress_rotate_min_interval_s == 0.0
    monkeypatch.setenv("ZEN_EGRESS_ROTATE_MIN_INTERVAL_S", "600")
    assert config.load_settings().zen_egress_rotate_min_interval_s == 600.0


def test_zen_model_map_parsing(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.setenv(
        "ZEN_MODEL_MAP",
        "deepseek-ai/deepseek-v4.1-flash=deepseek-v4.1-flash-free, big-pickle=big-pickle",
    )
    s = config.load_settings()
    assert s.zen_model_map == {
        "deepseek-ai/deepseek-v4.1-flash": "deepseek-v4.1-flash-free",
        "big-pickle": "big-pickle",
    }
    # malformed entries are skipped, not fatal
    monkeypatch.setenv("ZEN_MODEL_MAP", "broken, ok=fine")
    s = config.load_settings()
    assert s.zen_model_map == {"ok": "fine"}


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
    # The admin default is a *published* placeholder (public repo), so it must
    # not be allowed to guard a network-exposed gateway either.
    monkeypatch.delenv("ADMIN_API_KEYS", raising=False)
    with pytest.raises(ValueError, match="ADMIN_API_KEYS"):
        config.load_settings()
    monkeypatch.setenv("ADMIN_API_KEYS", "adm1")
    s = config.load_settings()
    assert s.host == "0.0.0.0"
    assert not s.dev_open
    assert s.admin_api_keys == ["adm1"]


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


def test_stream_stall_s_default_env_and_disable(monkeypatch):
    """The stream stall watchdog must default on (bounded well below the
    300s read timeout that let a dead tunnel cut clients on 2026-10-19)."""
    monkeypatch.delenv("STREAM_STALL_S", raising=False)
    assert config.load_settings().stream_stall_s == 60
    monkeypatch.setenv("STREAM_STALL_S", "15")
    assert config.load_settings().stream_stall_s == 15
    monkeypatch.setenv("STREAM_STALL_S", "0")  # explicit opt-out
    assert config.load_settings().stream_stall_s == 0


def test_client_stall_s_default_env_and_disable(monkeypatch):
    """The client-write watchdog defaults on: on 2026-10-19 a blackholed
    client leg held a stream open for the full 600s hermes timeout while
    upstream kept producing (see docs/OPERATIONS.md §4.3)."""
    monkeypatch.delenv("CLIENT_STALL_S", raising=False)
    assert config.load_settings().client_stall_s == 60
    monkeypatch.setenv("CLIENT_STALL_S", "15")
    assert config.load_settings().client_stall_s == 15
    monkeypatch.setenv("CLIENT_STALL_S", "0")  # explicit opt-out
    assert config.load_settings().client_stall_s == 0


def test_admin_default_is_a_placeholder():
    """This repo is PUBLIC: the built-in admin key must be an obvious fake.

    A real value here would be a published credential that authenticates
    against every gateway that did not override ADMIN_API_KEYS.
    """
    assert config.DEFAULT_ADMIN_API_KEYS == ("dev-admin-key",)


def test_empty_bool_env_falls_back_to_default(monkeypatch):
    """`set MASK_MODELS=` (empty) used to disable masking silently, which
    leaked upstream model names. Empty now means "unset"."""
    monkeypatch.setenv("MASK_MODELS", "")
    assert config.load_settings().mask_models is True
    monkeypatch.setenv("MASK_MODELS", "false")
    assert config.load_settings().mask_models is False


def test_builtin_public_models_use_env_parsing():
    """The dataclass default must split identically to PUBLIC_MODELS."""
    s = config.Settings()
    assert s.public_models == config._parse_public_models(
        ",".join(config.DEFAULT_PUBLIC_MODELS)
    )


def test_ttfb_deadline_default_and_env(monkeypatch):
    """The header deadline is separate from the 300s whole-request budget.

    2026-10-25: without it a blackholed egress hop cost 305-315s per client
    request before the failover ladder could try another bucket.
    """

    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.delenv("ZEN_TTFB_S", raising=False)
    s = config.load_settings()
    assert s.zen_ttfb_s == 60.0
    assert s.zen_ttfb_s < s.zen_timeout_s

    monkeypatch.setenv("ZEN_TTFB_S", "12.5")
    assert config.load_settings().zen_ttfb_s == 12.5
    # 0 disables the extra timer (legacy behaviour)
    monkeypatch.setenv("ZEN_TTFB_S", "0")
    assert config.load_settings().zen_ttfb_s == 0.0


def test_ttfb_demoted_is_shorter_than_the_full_deadline(monkeypatch):
    """A demoted bucket gets a strict header deadline (owner 2026-10-29).

    The full ZEN_TTFB_S protects a legitimately slow first attempt; a leg that
    already failed must not be granted the same patience twice, or the user
    waits a second minute before the backup chain answers.
    """

    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.delenv("ZEN_TTFB_S", raising=False)
    monkeypatch.delenv("ZEN_TTFB_DEMOTED_S", raising=False)
    s = config.load_settings()
    assert s.zen_ttfb_demoted_s == 15.0
    assert s.zen_ttfb_demoted_s < s.zen_ttfb_s

    monkeypatch.setenv("ZEN_TTFB_DEMOTED_S", "8")
    assert config.load_settings().zen_ttfb_demoted_s == 8.0
    # 0 disables the distinction (every attempt uses ZEN_TTFB_S)
    monkeypatch.setenv("ZEN_TTFB_DEMOTED_S", "0")
    assert config.load_settings().zen_ttfb_demoted_s == 0.0
    # never negative
    monkeypatch.setenv("ZEN_TTFB_DEMOTED_S", "-5")
    assert config.load_settings().zen_ttfb_demoted_s == 0.0


def test_egress_cooldown_defaults_and_env(monkeypatch):
    """Demotion window drives the "do not bench on one failure" rule."""

    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.delenv("ZEN_EGRESS_COOLDOWN_S", raising=False)
    monkeypatch.delenv("ZEN_EGRESS_COOLDOWN_MAX_S", raising=False)
    s = config.load_settings()
    assert s.zen_egress_cooldown_s == 20.0
    # 0.8.5: a transport failure never cordons, so the demotion may escalate
    # all the way to the old cordon length (300s) instead of stopping short.
    assert s.zen_egress_cooldown_max_s == 300.0
    assert s.zen_egress_cooldown_s <= s.zen_egress_cooldown_max_s

    monkeypatch.setenv("ZEN_EGRESS_COOLDOWN_S", "7")
    monkeypatch.setenv("ZEN_EGRESS_COOLDOWN_MAX_S", "9")
    s = config.load_settings()
    assert s.zen_egress_cooldown_s == 7.0
    assert s.zen_egress_cooldown_max_s == 9.0


def test_quota_cordon_cap_defaults_match_the_pool(monkeypatch):
    """The 429 bench cap must default to 600 everywhere it is declared.

    Regression: ``Settings`` defaulted to 1800 (and load_settings read "1800"
    from the environment) while ``EgressPool`` defaulted to 600 and the docs
    plus ``deploy/ecs/env.example`` had already retired 1800 — a deployment
    that simply never set ZEN_QUOTA_CORDON_MAX_S ran the value the post-mortem
    had rejected (on 2026-10-29 a pool-wide short-window throttle stretched
    ~12 minutes of outage into 30 minutes of 503s).
    """

    from proxy_opencode.egress.pool import EgressPool, EgressSpec

    spec = EgressSpec("a", "http://a:10001")
    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.delenv("ZEN_QUOTA_CORDON_MAX_S", raising=False)
    s = config.load_settings()
    assert s.zen_quota_cordon_max_s == 600.0
    assert s.zen_quota_cordon_max_s == config.Settings().zen_quota_cordon_max_s
    assert EgressPool([spec])._quota_cordon_max_s == 600.0  # noqa: SLF001

    monkeypatch.setenv("ZEN_QUOTA_CORDON_MAX_S", "0")
    assert config.load_settings().zen_quota_cordon_max_s == 0.0


def test_fallback_recover_after_floor(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MODE", "zen-direct")
    monkeypatch.delenv("ZEN_FALLBACK_RECOVER_AFTER", raising=False)
    assert config.load_settings().zen_fallback_recover_after == 2
    # A nonsense value must not disable recovery entirely.
    monkeypatch.setenv("ZEN_FALLBACK_RECOVER_AFTER", "0")
    assert config.load_settings().zen_fallback_recover_after == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://172.17.0.1:10003", "http://172.17.0.1:10003"),
        ("http://172.17.0.1:10003/", "http://172.17.0.1:10003"),
        # Measured 2026-10-28: this spelling composed /v1/v1/chat/completions,
        # so the backup answered 404 and a 429 primary looked unfixable.
        ("http://172.17.0.1:10003/v1", "http://172.17.0.1:10003"),
        ("http://172.17.0.1:10003/v1/", "http://172.17.0.1:10003"),
        ("  https://api.modelscope.cn/v1  ", "https://api.modelscope.cn"),
        # Only the trailing version segment is stripped, never a deeper path.
        ("https://gw.example.comrelay/v1", "https://gw.example.comrelay"),
        ("", ""),
    ],
)
def test_fallback_base_url_is_normalised(raw, expected):
    assert config._api_base(raw) == expected


def test_fallback_base_url_v1_suffix_does_not_double_the_path(monkeypatch):
    monkeypatch.setenv("ZEN_FALLBACK_BASE_URL", "http://172.17.0.1:10003/v1")
    settings = config.load_settings()
    assert settings.zen_fallback_base_url == "http://172.17.0.1:10003"
