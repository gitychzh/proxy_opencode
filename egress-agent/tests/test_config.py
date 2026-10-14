"""Tests for egress_agent.config (P0 scope)."""

from __future__ import annotations

import json

import pytest
from egress_agent.config import AgentConfig, ConfigError, OntConfig


def _write(tmp_path, data) -> str:
    p = tmp_path / "agent.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_defaults_are_valid() -> None:
    cfg = AgentConfig.from_mapping({})
    assert cfg.listener.port == 10001
    assert cfg.listener.host == "0.0.0.0"
    assert cfg.egress.name == "default"
    assert cfg.egress.bind_source is None
    assert cfg.onts == ()


def test_load_from_file_round_trip(tmp_path) -> None:
    data = {
        "listener": {"host": "0.0.0.0", "port": 10002},
        "egress": {"name": "opc2-eth", "bind_source": "192.168.1.222"},
        "onts": [
            {
                "name": "v173-50",
                "kind": "huawei",
                "url": "http://192.168.1.1",
                "interface": "enp12s0",
                "username": "user",
                "password": "s3cret-not-in-repo",
            }
        ],
    }
    cfg = AgentConfig.load(_write(tmp_path, data))
    assert cfg.listener.port == 10002
    assert cfg.egress.bind_source == "192.168.1.222"
    assert cfg.onts[0].name == "v173-50"
    assert cfg.onts[0].password == "s3cret-not-in-repo"


def test_missing_file_is_config_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        AgentConfig.load(tmp_path / "nope.json")


def test_invalid_json_is_config_error(tmp_path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        AgentConfig.load(str(p))


def test_port_out_of_range() -> None:
    with pytest.raises(ConfigError, match="out of range"):
        AgentConfig.from_mapping({"listener": {"port": 70000}})


def test_auth_must_be_paired() -> None:
    with pytest.raises(ConfigError, match="must be set together"):
        AgentConfig.from_mapping({"listener": {"username": "u"}})


def test_empty_egress_name_rejected() -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        AgentConfig.from_mapping({"egress": {"name": "  "}})


def test_duplicate_ont_names_rejected() -> None:
    data = {"onts": [{"name": "a"}, {"name": "a"}]}
    with pytest.raises(ConfigError, match="duplicate"):
        AgentConfig.from_mapping(data)


def test_ont_url_must_be_http() -> None:
    data = {"onts": [{"name": "a", "url": "192.168.1.1"}]}
    with pytest.raises(ConfigError, match="must be http"):
        AgentConfig.from_mapping(data)


def test_echo_url_must_be_http() -> None:
    with pytest.raises(ConfigError, match="must be http"):
        AgentConfig.from_mapping({"echo_url": "myip.ipip.net"})


def test_summary_redacts_ont_password() -> None:
    cfg = AgentConfig.from_mapping(
        {
            "egress": {"name": "cc9", "bind_source": None},
            "listener": {"port": 10001, "username": "u", "password": "p"},
            "onts": [{"name": "ont", "password": "topsecret"}],
        }
    )
    summary = cfg.summary()
    assert summary["egress_name"] == "cc9"
    assert summary["proxy_auth"] is True
    blob = json.dumps(summary)
    assert "topsecret" not in blob
    assert "password" not in blob  # secret-free by construction
    assert summary["onts"][0]["username"] == "user"


def test_ontconfig_redacted_shape() -> None:
    ont = OntConfig(name="x", password="p")
    red = ont.redacted()
    assert set(red) == {"name", "kind", "url", "interface", "username", "enabled"}
