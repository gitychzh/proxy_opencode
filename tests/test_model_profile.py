"""Model parameters must equal the opencode client's own numbers.

Every expectation here is a transcription of the opencode source tree
(``sst/opencode`` @ dev, 2026-10-27) — if one of these fails, the gateway has
silently stopped agreeing with the client it stands in for, which shows up in
production as a client that never compacts and a provider that keeps refusing.
"""

from __future__ import annotations

import pytest

from proxy_opencode.model_profile import (
    OUTPUT_TOKEN_MAX,
    Limits,
    compaction_ceiling,
    estimate_request,
    estimate_text,
    output_budget,
    profile,
    profiles,
)


def test_big_pickle_limits_match_the_live_upstream_not_the_stale_snapshot():
    # models-dev/snapshot.txt ships {context 200000, input 160000, output 32000},
    # but the live upstream enforces 262139 and rejects past it with
    # "Input token count (N) exceeds the model's maximum context length of
    #  (262139), no tokens left for generation" (measured 2026-10-29).
    # The provider's live numbers win: this module's contract is to mirror the
    # provider, and the snapshot is demonstrably stale.
    prof = profile("big-pickle")
    assert prof is not None
    assert prof.limits.context == 262_139
    assert prof.limits.input == 261_000
    assert prof.limits.output == 32_000
    # A prompt at the guard limit must still leave room for the 1024-token
    # output floor, otherwise the upstream rejects prompt+max_tokens.
    assert prof.limits.input < prof.limits.context


def test_big_pickle_declares_reasoning_without_effort_levels():
    # capabilities.reasoning = {supported: true} — no "effort", no "toggle".
    prof = profile("big-pickle")
    assert prof is not None
    assert prof.capabilities.reasoning is True
    assert prof.capabilities.efforts == ()


def test_compaction_ceiling_is_input_minus_15_percent_for_big_pickle():
    # calculateCeiling: 160000 - max(24000, 16000) == 136000
    assert compaction_ceiling(Limits(context=200_000, input=160_000, output=32_000)) == 136_000


def test_compaction_ceiling_uses_context_when_no_input_limit_is_published():
    # deepseek-v4.1-flash-free publishes context only: 200000 - 30000 == 170000
    assert compaction_ceiling(Limits(context=200_000, output=32_000)) == 170_000


def test_compaction_ceiling_keeps_only_the_share_for_a_small_window():
    # A window below 2*RESERVE_MIN cannot give up 16k, so only 15% is kept.
    assert compaction_ceiling(Limits(context=20_000)) == 17_000


def test_compaction_ceiling_is_zero_when_nothing_is_published():
    assert compaction_ceiling(Limits()) == 0


def test_token_estimate_matches_opencode_chars_per_token():
    # util/token.ts: round(len / 4)
    assert estimate_text("a" * 400) == 100
    assert estimate_text("") == 0
    assert estimate_text(None) == 0


def test_request_estimate_counts_tool_schemas():
    messages = [{"role": "user", "content": "x" * 400}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "y" * 400,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    without_tools = estimate_request(messages)
    with_tools = estimate_request(messages, tools)
    assert with_tools > without_tools
    assert without_tools == 100


def test_output_budget_takes_the_catalogue_limit_when_the_prompt_is_small():
    assert output_budget(Limits(context=200_000, input=160_000, output=32_000), 1_000) == 32_000


def test_output_budget_shrinks_to_the_room_left_in_the_window():
    # room = 200000 - 150000 - ceil(150000*1.15) = -122500 -> floored to 1024
    assert output_budget(Limits(context=200_000, output=32_000), 150_000) == 1_024


def test_output_budget_is_capped_by_the_global_maximum():
    assert output_budget(Limits(context=1_000_000, output=512_000), 0) == OUTPUT_TOKEN_MAX


def test_profile_lookup_tolerates_a_provider_prefix():
    assert profile("opencode/big-pickle") is not None
    assert profile("big-pickle") is not None
    assert profile("nope") is None
    assert profile(None) is None


def test_profile_table_only_contains_free_relay_ids():
    # The gateway relays free ids only (AGENTS.md 模型门); a paid id in the
    # table would advertise limits for something it can never call.
    assert all(mid.endswith("-free") or mid == "big-pickle" for mid in profiles())


@pytest.mark.parametrize("model_id", sorted(profiles()))
def test_every_profile_is_self_consistent(model_id):
    prof = profiles()[model_id]
    assert prof.limits.window() > 0
    assert prof.limits.output > 0
    assert prof.ceiling() < prof.limits.window()
