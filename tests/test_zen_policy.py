"""The client-facing parameter policy: what the gateway forwards, refuses, fits.

Rules under test come from the opencode source tree (see ``zen/policy.py``):
a model without published effort levels never receives a client-chosen
thinking strength, the output budget is the one the CLI computes, and an
oversized prompt is refused locally with the shape that starts a compaction.
"""

from __future__ import annotations

import json

import pytest

from proxy_opencode.errors import ContextOverflow
from proxy_opencode.model_profile import profile
from proxy_opencode.upstreams.zen.policy import apply_request_policy

BIG_PICKLE = profile("big-pickle")
DEEPSEEK = profile("deepseek-v4.1-flash-free")


def _body(**extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "big-pickle",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(extra)
    return body


# ------------------------------------------------------------ reasoning


def test_model_without_effort_levels_drops_the_clients_choice():
    body = _body(reasoning_effort="high")
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert "reasoning_effort" not in body
    assert applied.dropped == ("reasoning_effort",)


def test_every_thinking_control_shape_is_dropped_for_big_pickle():
    body = _body(
        reasoning_effort="low",
        thinking={"type": "enabled", "budget_tokens": 4096},
        reasoning={"effort": "high"},
        include_reasoning=True,
    )
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert set(applied.dropped) == {
        "reasoning_effort",
        "thinking",
        "reasoning",
        "include_reasoning",
    }
    for field in applied.dropped:
        assert field not in body


def test_model_that_publishes_efforts_keeps_a_valid_choice():
    body = _body(reasoning_effort="high")
    applied = apply_request_policy(body, profile=DEEPSEEK)
    assert body["reasoning_effort"] == "high"
    assert applied.dropped == ()


def test_model_that_publishes_efforts_still_drops_an_unknown_level():
    body = _body(reasoning_effort="ultra")
    applied = apply_request_policy(body, profile=DEEPSEEK)
    assert "reasoning_effort" not in body
    assert applied.dropped == ("reasoning_effort",)


def test_client_policy_mode_is_the_legacy_passthrough():
    body = _body(reasoning_effort="high")
    applied = apply_request_policy(
        body, profile=BIG_PICKLE, reasoning_policy="client"
    )
    assert body["reasoning_effort"] == "high"
    assert applied.dropped == ()


def test_off_mode_strips_controls_even_when_the_model_publishes_efforts():
    body = _body(reasoning_effort="high")
    applied = apply_request_policy(body, profile=DEEPSEEK, reasoning_policy="off")
    assert "reasoning_effort" not in body
    assert applied.dropped == ("reasoning_effort",)


def test_unknown_policy_name_is_rejected_at_the_boundary():
    with pytest.raises(ValueError, match="unknown reasoning policy"):
        apply_request_policy(_body(), profile=BIG_PICKLE, reasoning_policy="loud")


# ----------------------------------------------------------- output budget


def test_absent_max_tokens_is_filled_with_the_cli_budget():
    body = _body()
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_tokens"] == 32_000
    assert applied.max_tokens == 32_000
    assert applied.clamped is False


def test_oversized_max_tokens_is_clamped_to_the_window():
    body = _body(max_tokens=200_000)
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_tokens"] == 32_000
    assert applied.clamped is True


def test_smaller_client_budget_is_left_alone():
    body = _body(max_tokens=512)
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_tokens"] == 512
    assert applied.clamped is False


def test_numeric_string_budget_is_normalised_to_int():
    body = _body(max_tokens="8000")
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_tokens"] == 8_000
    assert isinstance(body["max_tokens"], int)
    assert applied.max_tokens == 8_000
    assert applied.clamped is False


def test_numeric_string_budget_above_budget_is_clamped():
    body = _body(max_tokens="800000")
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_tokens"] == 32_000
    assert applied.clamped is True


def test_uninterpretable_budget_is_replaced_with_the_budget():
    for garbage in (True, "8e3", 8000.5, ["8000"]):
        body = _body(max_tokens=garbage)
        applied = apply_request_policy(body, profile=BIG_PICKLE)
        assert body["max_tokens"] == 32_000, garbage
        assert applied.max_tokens == 32_000
        assert applied.clamped is True, garbage


def test_integral_float_budget_is_normalised_to_int():
    body = _body(max_tokens=512.0)
    applied = apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_tokens"] == 512
    assert isinstance(body["max_tokens"], int)
    assert applied.clamped is False


def test_max_completion_tokens_is_the_field_that_gets_fitted():
    body = _body(max_completion_tokens=999_999)
    apply_request_policy(body, profile=BIG_PICKLE)
    assert body["max_completion_tokens"] == 32_000
    assert "max_tokens" not in body


def test_clamp_mode_never_adds_a_budget_the_client_did_not_ask_for():
    body = _body()
    apply_request_policy(body, profile=BIG_PICKLE, output_budget_mode="clamp")
    assert "max_tokens" not in body


def test_off_mode_leaves_the_field_untouched():
    body = _body(max_tokens=200_000)
    apply_request_policy(body, profile=BIG_PICKLE, output_budget_mode="off")
    assert body["max_tokens"] == 200_000


# ---------------------------------------------------------- context guard


def test_prompt_past_the_input_limit_is_refused_locally():
    body = _body(messages=[{"role": "user", "content": "x" * 1_200_000}])
    with pytest.raises(ContextOverflow) as excinfo:
        apply_request_policy(body, profile=BIG_PICKLE)
    err = excinfo.value
    assert err.input_limit == 261_000
    assert err.estimated_tokens > 261_000
    assert "261000" in str(err)


def test_guard_measures_the_composed_body_including_injected_tools():
    # The estimate must cover tools too: the free tier injects its own set, so
    # a body that fits on messages alone can still overflow once composed.
    tools = [
        {
            "type": "function",
            "function": {"name": "bash", "description": "y" * 600_000},
        }
    ]
    body = _body(messages=[{"role": "user", "content": "x" * 600_000}])
    body["tools"] = tools
    with pytest.raises(ContextOverflow):
        apply_request_policy(body, profile=BIG_PICKLE)


def test_guard_can_be_disabled():
    body = _body(messages=[{"role": "user", "content": "x" * 700_000}])
    applied = apply_request_policy(body, profile=BIG_PICKLE, context_guard=False)
    assert applied.estimated_input > 160_000


def test_input_limit_override_moves_the_boundary():
    # 1_200_000 chars / 4 == 300_000 tokens: past the override, not past 160k.
    body = _body(messages=[{"role": "user", "content": "x" * 1_200_000}])
    with pytest.raises(ContextOverflow) as excinfo:
        apply_request_policy(body, profile=BIG_PICKLE, input_limit_override=250_000)
    assert excinfo.value.input_limit == 250_000


def test_unknown_model_gets_no_invented_limits():
    body = _body(messages=[{"role": "user", "content": "x" * 900_000}], max_tokens=500_000)
    applied = apply_request_policy(body, profile=None)
    assert applied == applied.__class__(dropped=())
    assert body["max_tokens"] == 500_000


# ------------------------------------------------------- route integration


def test_context_overflow_answers_with_the_openai_context_code():
    """A 400 with `context_length_exceeded` is what makes a client compact."""

    from fastapi.testclient import TestClient

    from proxy_opencode.app import create_app
    from proxy_opencode.config import Settings
    from proxy_opencode.errors import ContextOverflow

    class OverflowAdapter:
        name = "opencode-serve"

        async def list_models(self):
            return {"object": "list", "data": []}

        async def chat(self, payload):
            raise ContextOverflow(
                model="big-pickle",
                estimated_tokens=170_000,
                input_limit=160_000,
                context_limit=200_000,
                compaction_ceiling=136_000,
            )

    settings = Settings(
        upstream_mode="opencode-serve",
        gateway_api_keys=["gw-key"],
        opencode_server_password="pw",
    )
    app = create_app(settings)
    app.state.adapter = OverflowAdapter()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer gw-key"},
            json={
                "model": "deepseek-ai/deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "context_length_exceeded"
    assert "136000" in error["message"]
    assert json.dumps(error)  # JSON-serialisable, no exception objects leaked
