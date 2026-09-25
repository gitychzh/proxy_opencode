"""JSON tool-call contract bridging for the opencode-serve adapter.

The local `opencode serve` prompt API accepts plain text only - external
(OpenAI-style) function-calling tools cannot be registered with it. We
therefore teach the (tool-capable, reasoning) model a small JSON contract:

  * tools present -> system addendum describes the tools and requires replies
    to be EITHER a plain assistant message OR a single JSON object:
        {"tool_calls": [{"name": "...", "arguments": {...}}]}
  * the assistant's previous tool calls are replayed as that same JSON
  * role=tool results are replayed as `tool_result(name): <content>`

The assistant text is then parsed back into OpenAI `message.tool_calls`.
Anything that does not parse stays plain content - never silently dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any

CONTRACT_HEADER = """You are being called through an OpenAI-compatible API that supports
function calling. The caller program executes tools for you; you must
request them by emitting JSON.
RULES (absolute, no exceptions):
1. If ANY tool is needed to fulfill the request, your ENTIRE reply must be
   exactly one JSON object and nothing else: no markdown fences, no prose,
   no thinking-out-loud, no preamble:
   {"tool_calls": [{"name": "<tool_name>", "arguments": {...}}]}
2. arguments MUST be a JSON object matching the tool parameters schema.
3. NEVER announce actions ("let me run ...", "I will create ...").
   Describing an action is a failure: actions happen ONLY via tool_calls JSON.
4. Never invent or claim tool results. After tool_calls, the caller runs the
   tools and returns tool_result messages.
5. Only when NO tool is needed, answer in plain natural-language text (NOT JSON).
Example:
user: create file x.txt containing hi
assistant: {"tool_calls": [{"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}]}
"""

_REASONING_HINTS = {
    "minimal": "Keep internal reasoning extremely brief.",
    "low": "Keep internal reasoning brief.",
    "medium": "",
    "high": "Think step by step, thoroughly, before answering.",
    "max": "Think step by step, with maximum depth, before answering.",
}


def _msg_text(content: Any) -> str:
    """Extract text from an OpenAI message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _tool_addendum(tools: list[dict[str, Any]], tool_choice: Any) -> str:
    specs = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        specs.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    lines = [CONTRACT_HEADER, "TOOLS:", json.dumps(specs, ensure_ascii=False)]
    if tool_choice == "required":
        lines.append("You MUST call at least one tool in your reply.")
    elif isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if fn.get("name"):
            lines.append(f'You MUST call the tool {fn["name"]!r} in your reply.')
    return "\n".join(lines)


def build_prompt(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    reasoning_effort: str | None = None,
) -> str:
    """Flatten an OpenAI chat history (+ tool contract) into one prompt."""
    parts: list[str] = []
    if tools:
        parts.append("system:\n" + _tool_addendum(tools, tool_choice))
    if reasoning_effort in _REASONING_HINTS and _REASONING_HINTS[reasoning_effort]:
        parts.append("system:\n" + _REASONING_HINTS[reasoning_effort])

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            text = _msg_text(msg.get("content"))
            if text.strip():
                parts.append(f"system:\n{text}")
        elif role == "user":
            text = _msg_text(msg.get("content"))
            if text.strip():
                parts.append(f"user:\n{text}")
        elif role == "assistant":
            text = _msg_text(msg.get("content"))
            calls = msg.get("tool_calls") or []
            if calls:
                flat = {
                    "tool_calls": [
                        {
                            "name": (c.get("function") or {}).get("name"),
                            "arguments": _loads_args(
                                (c.get("function") or {}).get("arguments")
                            ),
                        }
                        for c in calls
                        if isinstance(c, dict)
                    ]
                }
                parts.append("assistant:\n" + json.dumps(flat, ensure_ascii=False))
            elif text.strip():
                parts.append(f"assistant:\n{text}")
        elif role == "tool":
            name = msg.get("name") or msg.get("tool_call_id") or "tool"
            parts.append(f"tool_result({name}):\n{_msg_text(msg.get('content'))}")

    if not any(p.startswith(("user:", "tool_result")) for p in parts):
        raise ValueError("messages contain no usable user content")
    return "\n\n".join(parts)


def _loads_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}
    return {}


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_tool_calls(text: str) -> list[dict[str, Any]] | None:
    """Parse the JSON tool-call contract out of an assistant reply.

    Returns OpenAI-style tool_calls, or None when the reply is plain text.
    """
    if not text:
        return None
    stripped = text.strip()
    candidates = [stripped]
    fenced = _FENCE_RE.search(stripped)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.insert(0, stripped[start : end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:
            continue
        calls = data.get("tool_calls") if isinstance(data, dict) else None
        if not isinstance(calls, list) or not calls:
            continue
        out: list[dict[str, Any]] = []
        for i, call in enumerate(calls):
            if not isinstance(call, dict) or not call.get("name"):
                out = []
                break
            args = call.get("arguments")
            out.append(
                {
                    "id": f"call_bridge_{i}",
                    "type": "function",
                    "function": {
                        "name": str(call["name"]),
                        "arguments": args
                        if isinstance(args, str)
                        else json.dumps(args or {}, ensure_ascii=False),
                    },
                }
            )
        if out:
            return out
    return None
