"""Tests for egress_agent.health."""

from __future__ import annotations

import json

from egress_agent.health import HealthState, extract_ip


def _state() -> HealthState:
    return HealthState(egress_name="opc2-wifi", bind_source=None, echo_url="http://x")


def test_extract_ip_ipv4_from_chinese_echo() -> None:
    assert extract_ip("当前 IP：117.95.231.70  来自于：中国 江苏 宿迁  电信") == "117.95.231.70"


def test_extract_ip_ipv6() -> None:
    text = "current ip 2408:823c:be11:313d:a726:5525:b61f:8f54 done"
    assert extract_ip(text) == "2408:823c:be11:313d:a726:5525:b61f:8f54"


def test_extract_ip_plain_text_echo() -> None:
    assert extract_ip("36.149.54.158\n") == "36.149.54.158"


def test_extract_ip_no_ip() -> None:
    assert extract_ip("no address here at all") is None


def test_payload_shape_and_counters() -> None:
    state = _state()
    state.tunnels_active = 2
    state.tunnels_total = 7
    payload = state.payload()
    assert payload["egress"] == "opc2-wifi"
    assert payload["ready"] is True
    assert payload["tunnels_active"] == 2
    assert payload["tunnels_total"] == 7
    assert payload["egress_ip"] is None
    assert "uptime_s" in payload
    # secret-free by construction
    blob = json.dumps(payload)
    assert "password" not in blob


def test_note_rotation_clears_stale_ip() -> None:
    state = _state()
    state.egress_ip = "1.2.3.4"
    state.note_rotation(ok=True)
    assert state.egress_ip is None
    assert state.last_rotation_ok is True
    assert state.last_rotation_at is not None
