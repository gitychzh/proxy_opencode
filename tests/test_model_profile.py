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


def test_table_covers_every_free_id_in_the_catalogue():
    # One profile per free id the gateway relays. 2026-11-17 the anonymous
    # catalogue gained `hy4-flash-free` (86 entries, 12 free; minimax left).
    # minimax's profile is deliberately retained for in-flight clients (the
    # gateway is passthrough — the id still reaches upstream verbatim), and
    # hy4 is added from the live catalogue. A missing id means no context
    # guard and no reasoning forwarding.
    expected = {
        "big-pickle",
        "mimo-v2.6-flash-free",
        "deepseek-v4.1-flash-free",
        "glm-5.3-flash-free",
        "longcat-2.5-free",
        "iceman-free",
        "inkling-2-small-free",
        "beam-free",
        "flint-40b-free",
        "nemotron-3.5-super-vl-free",
        "nemotron-3.5-lightning-free",
        "minimax-m3.1-flash-free",  # delisted upstream, retained for in-flight clients
        "hy4-flash-free",
    }
    assert set(profiles()) == expected


def test_hy4_flash_publishes_the_live_window_and_effort_ladder():
    # Live anonymous catalogue @2026-11-17 (canonical tencent/hy4-flash):
    # context 1,048,576 / input 983,040 / output 65,536, reasoning efforts
    # [none, high], four image input modalities. Measured 2026-11-17:
    # natural CJK 1,281,215 chars -> 867,947 usage tokens all 200; 4-byte
    # U+20000 94,009 chars -> 384,264 tokens. Not subject to the 32000-about
    # estimator gate that caps the other free ids at ~94.4K chars (§11).
    prof = profile("hy4-flash-free")
    assert prof is not None
    assert prof.limits.context == 1_048_576
    assert prof.limits.input == 983_040
    assert prof.limits.output == 65_536
    assert prof.capabilities.reasoning is True
    assert prof.capabilities.efforts == ("none", "high")
    assert prof.capabilities.attachment is True
    # "none" is a client-selectable effort; thinking is default-on.
    assert "none" in prof.capabilities.efforts and prof.capabilities.reasoning


def test_delisted_model_profile_is_not_kept():
    # muse-spark-1.3-contributor-free left the upstream catalogue; keeping
    # its row would advertise limits for a dead id.
    assert profile("muse-spark-1.3-contributor-free") is None


def test_minimax_publishes_the_one_million_window_and_effort_ladder():
    # models.dev 2026-11-14: context 1,000,000 / output 512,000, efforts
    # [low, medium, high, xhigh, max]. Live 2026-11-14: exactly 1,000,000
    # prompt tokens accepted, 1,080,000 rejected ("context window exceeds
    # limit" — the vendor's own message).
    prof = profile("minimax-m3.1-flash-free")
    assert prof is not None
    assert prof.limits.context == 1_000_000
    assert prof.limits.output == 512_000
    assert prof.capabilities.efforts == ("low", "medium", "high", "xhigh", "max")
    assert prof.capabilities.attachment is True


def test_iceman_publishes_no_streamed_reasoning_field_and_no_temperature():
    # models.dev 2026-11-14: iceman has no `interleaved` block, no
    # temperature / structured_output keys — all three mean "absent".
    prof = profile("iceman-free")
    assert prof is not None
    assert prof.capabilities.reasoning_field == ""
    assert prof.capabilities.temperature is False
    assert prof.capabilities.structured_output is False
    assert prof.capabilities.efforts == ()


def test_longcat_publishes_no_temperature_control():
    prof = profile("longcat-2.5-free")
    assert prof is not None
    assert prof.capabilities.temperature is False
    # reasoning has a toggle upstream but no strength ladder.
    assert prof.capabilities.efforts == ()


def test_models_supporting_thinking_off_declare_none():
    # inkling-2-small / nemotron-3.5-super-vl publish "none": the client may
    # switch thinking OFF, so the gateway must forward that level too.
    for mid in ("inkling-2-small-free", "nemotron-3.5-super-vl-free"):
        prof = profile(mid)
        assert prof is not None
        assert "none" in prof.capabilities.efforts


@pytest.mark.parametrize("model_id", sorted(profiles()))
def test_every_profile_is_self_consistent(model_id):
    prof = profiles()[model_id]
    assert prof.limits.window() > 0
    assert prof.limits.output > 0
    assert prof.ceiling() < prof.limits.window()


# ------------------------------------------ billed prompt accounting (usage)
#
# The free tier bills prompts two ways: the *gate* is content-agnostic
# (`round(chars * 0.339) <= 32000`), the *bill* is the vendor tokenizer's
# `usage.prompt_tokens`. Streaming answers carry no usage at all upstream
# (2026-11-18), so the relay synthesises one from the bill-shaped estimator.
# These expectations pin it to the two live bisections of the same 92,675-char
# Chinese prompt: 62,807 billed tokens (2026-11-18) and 71,238 (2026-11-17).

_MEASURED_PROMPT = "码头台账记载了当日到港的货物清单与验收情况，由军需官签字确认。" * 2_990


def test_estimate_upstream_tokens_tracks_the_billed_prompt():
    from proxy_opencode.model_profile import estimate_upstream_tokens

    chars = len(_MEASURED_PROMPT)
    assert 92_000 <= chars <= 93_000
    estimate = estimate_upstream_tokens(
        [{"role": "user", "content": _MEASURED_PROMPT}]
    )
    # Both live samples live inside this band (65k midpoint +-10%).
    assert 58_000 <= estimate <= 72_000


def test_estimate_upstream_tokens_counts_tools_and_ascii_density():
    from proxy_opencode.model_profile import estimate_upstream_tokens

    text_only = estimate_upstream_tokens([{"role": "user", "content": "hi"}])
    with_tool = estimate_upstream_tokens(
        [{"role": "user", "content": "hi"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from disk.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert with_tool > text_only
    # ASCII/code is billed near 3.6 chars per token, not one per character.
    ascii_only = estimate_upstream_tokens(
        [{"role": "user", "content": "a" * 3_600}]
    )
    assert 900 <= ascii_only <= 1_500


def test_estimate_completion_tokens_prices_generated_text():
    from proxy_opencode.model_profile import estimate_completion_tokens

    assert estimate_completion_tokens(0) == 0
    assert 100 <= estimate_completion_tokens(200) <= 160
