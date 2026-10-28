"""Tool renames must be symmetric: request side renames, response side restores.

The body/shape gate forces every builtin name into ``tools``, so a client tool
called ``bash`` is renamed to ``client_bash`` on the way out. Before this test
existed the response side had no reverse map at all — a model replying
``client_bash`` handed the client a name it never declared, which either got
dropped or executed as a stranger.
"""

from __future__ import annotations

import json

from proxy_opencode.sse_mask import (
    _restore_patterns,
    mask_json_model,
    mask_model_field,
    rewrite_sse_line,
)
from proxy_opencode.upstreams.zen.tools import builtin_tools, rename_collisions


def _client_tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "client's own shell",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "never collides",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def test_rename_records_reverse_map():
    tools, _ = rename_collisions(_client_tools(), [], builtin_tools())
    names = [t["function"]["name"] for t in tools]
    assert "client_bash" in names
    assert "get_weather" in names

    from proxy_opencode.upstreams.zen.tools import restore_map

    assert restore_map() == {"client_bash": "bash"}


def test_non_stream_name_restored():
    rename_collisions(_client_tools(), [], builtin_tools())
    from proxy_opencode.upstreams.zen.tools import restore_map

    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "client_bash",
                                "arguments": '{"cmd": "ls"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    mask_json_model(body, "deepseek-ai/deepseek-v4.1-flash", restore_map())
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"


def test_stream_line_name_restored():
    rename_collisions(_client_tools(), [], builtin_tools())
    from proxy_opencode.upstreams.zen.tools import restore_map

    patterns = _restore_patterns(restore_map())
    line = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"client_bash"}}]}}]}'
    out = rewrite_sse_line(line, "deepseek-ai/deepseek-v4.1-flash", patterns)
    assert b'"name":"bash"' in out
    assert b"client_bash" not in out


async def test_mask_model_field_restores_names():
    rename_collisions(_client_tools(), [], builtin_tools())
    from proxy_opencode.upstreams.zen.tools import restore_map

    async def upstream():
        yield b'data: {"choices":[{"delta":{"function":{"name":"client_bash"}}}],"model":"x"}\n'
        yield b"data: [DONE]\n"

    out = b""
    async for chunk in mask_model_field(upstream(), "public-id", restore_map()):
        out += chunk
    assert b"client_bash" not in out
    assert b'"name":"bash"' in out
    assert b'"model":"public-id"' in out


def test_no_collision_leaves_body_untouched():
    tools, _ = rename_collisions(_client_tools(), [], builtin_tools())
    body = {"choices": [{"message": {"tool_calls": [{"function": {"name": "get_weather"}}]}}]}
    before = json.loads(json.dumps(body))
    mask_json_model(body, "m", {"client_bash": "bash"})
    assert (
        body["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        == (before["choices"][0]["message"]["tool_calls"][0]["function"]["name"])
    )
