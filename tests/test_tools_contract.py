"""Unit tests for the JSON tool-call contract bridge."""

from __future__ import annotations

import json

import pytest

from proxy_opencode.upstreams import tools_contract as tc


def _weather_tool():
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }


def test_build_prompt_flattens_roles():
    prompt = tc.build_prompt(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "assistant", "content": "Hi."},
            {"role": "user", "content": "2+2?"},
        ]
    )
    assert "system:\nBe terse." in prompt
    assert "user:\nHello" in prompt
    assert "assistant:\nHi." in prompt
    assert "user:\n2+2?" in prompt


def test_build_prompt_requires_user_content():
    with pytest.raises(ValueError):
        tc.build_prompt([{"role": "system", "content": "x"}])


def test_build_prompt_with_tools_injects_contract_and_replays_history():
    prompt = tc.build_prompt(
        [
            {"role": "user", "content": "weather in Paris?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
        tools=[_weather_tool()],
        tool_choice="auto",
        reasoning_effort="low",
    )
    assert "function calling" in prompt
    assert '"name": "get_weather"' in prompt
    assert "tool_result" in prompt
    assert "sunny" in prompt
    assert '"tool_calls"' in prompt  # replayed assistant tool call
    assert "brief" in prompt.lower()  # low-effort hint


def test_parse_tool_calls_plain_json():
    text = json.dumps(
        {"tool_calls": [{"name": "get_weather", "arguments": {"city": "Paris"}}]}
    )
    calls = tc.parse_tool_calls(text)
    assert calls is not None
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Paris"}


def test_parse_tool_calls_fenced_json():
    text = '```json\n{"tool_calls": [{"name": "t", "arguments": {}}]}\n```'
    calls = tc.parse_tool_calls(text)
    assert calls is not None and calls[0]["function"]["name"] == "t"


def test_parse_tool_calls_plain_text_returns_none():
    assert tc.parse_tool_calls("The weather is sunny today.") is None
    assert tc.parse_tool_calls('{"foo": 1}') is None
    assert tc.parse_tool_calls("") is None
