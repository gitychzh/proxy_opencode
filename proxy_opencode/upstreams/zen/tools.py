"""Free-tier tool-shape gate: injection of builtin tool schemas.

Gate rules, re-measured live against opencode 1.19.5 (2026-10-29 03:00 CST,
bun transport, one egress IP, one system prompt):

* the request must carry the **complete** builtin tool set of the current CLI
  (12 names as of 1.19.5). A strict subset fails with the quota-shaped
  ``429 FreeUsageLimitError`` — measured: keep only ``bash`` → 429, keep the
  old four-name subset → 429;
* extra tools are tolerated: the twelve builtins **plus** a custom
  ``run_shell_command`` → 200. That is what lets this gateway append the
  client's own tools after the builtins;
* ``tool_choice: auto`` is fine, ``max_tokens`` is not inspected (32, 4096 and
  32000 all passed).

Consequence (the 2026-10-29 02:00 outage): the shipped asset was a 1.19.4
capture whose builtin list had shrunk to four names — under the subset rule
every request came back as a fake quota 429 on all four egress IPs, which
looked exactly like an exhausted account. ``ZEN_TOOLS_MODE`` therefore
defaults to ``full``; ``minimal``/``captured2`` stay only as diagnosed
fallbacks.

Authority: AGENTS.md 硬约束 1 (形状门).
"""

from __future__ import annotations

import contextvars
import json
from pathlib import Path
from typing import Any

_ASSETS = Path(__file__).resolve().parent / "assets"
_ASSET_TOOLS = _ASSETS / "zen_builtin_tools.json"

MINIMAL_TOOL_NAMES = ("bash", "edit", "glob", "read")

# Minimum distinct builtin names the gate accepts.
MIN_INJECTED_TOOLS = 4

# Injected names, in preference order. A name the client already uses is
# skipped: injecting a duplicate would not add a distinct builtin name (the
# gate counts names, not entries) and dropping the builtin instead could push
# the count below the four-name minimum. The first four are the ones the live
# 1.19.4 client itself sends.
INJECT_NAME_POOL = (
    "bash", "edit", "glob", "read", "todowrite", "skill", "task",
    "list", "webfetch", "websearch", "grep", "write",
)


def builtin_tools() -> list[dict[str, Any]]:
    """The captured 11-tool asset, parsed (fresh read: it is a build asset)."""

    data = json.loads(_ASSET_TOOLS.read_text(encoding="utf-8"))
    return [t for t in data if isinstance(t, dict)]


def tool_names(tools: list[Any]) -> set[str]:
    """Names of a tool list (accepts both OpenAI and bare ``{name}`` shapes)."""

    names: set[str] = set()
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                names.add(str(fn["name"]))
            elif t.get("name"):
                names.add(str(t["name"]))
    return names


def minimal_schema(name: str) -> dict[str, Any]:
    """Smallest structurally valid function-tool schema for ``name``."""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def merge_tools(
    client_tools: list[dict[str, Any]],
    *,
    mode: str,
    builtins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Builtin schemas first (required by the gate), client tools after."""

    client_names = tool_names(client_tools)
    injected: list[dict[str, Any]]

    if mode in ("full", "all"):
        # Every builtin name must be present; the gate treats a subset as a
        # fake client. Client tools that shadow a builtin name are renamed by
        # ``rename_collisions`` before this call.
        injected = list(builtins)
    elif mode == "captured2":
        injected = [
            t for t in builtins
            if tool_names([t]) & set(MINIMAL_TOOL_NAMES)
            and not (tool_names([t]) & client_names)
        ]
    else:  # minimal
        injected = []
        for name in INJECT_NAME_POOL:
            if name in client_names:
                continue
            injected.append(minimal_schema(name))
            if len(injected) >= MIN_INJECTED_TOOLS:
                break
        if len(injected) < MIN_INJECTED_TOOLS:
            # The client claims every pool name (pathological). Fall back to
            # the captured subset so the gate still sees the names.
            injected = [
                t for t in builtins if tool_names([t]) & set(MINIMAL_TOOL_NAMES)
            ]
    return [*injected, *client_tools]


def rename_collisions(
    client_tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    builtins: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rename client tools that shadow a builtin name, and their tool_calls.

    The gate needs every builtin name present, so a client tool called
    ``bash`` cannot simply win the slot: it becomes ``client_bash`` and every
    historical ``tool_calls`` entry is rewritten to match, otherwise the model
    would be handed a call to a name that no longer exists in ``tools``.

    The rename is symmetric: the reverse map is stashed in a request-scoped
    contextvar so the response side can restore the client's own names. Without
    that half, a model replying ``client_bash`` hands the client a tool name it
    never declared — the call is dropped (or, worse, executed as a stranger).
    """

    reserved = tool_names(builtins)
    mapping: dict[str, str] = {}
    out_tools: list[dict[str, Any]] = []
    for tool in client_tools:
        name = next(iter(tool_names([tool])), "")
        if name and name in reserved:
            new_name = f"client_{name}"
            mapping[name] = new_name
            tool = json.loads(json.dumps(tool))
            if isinstance(tool.get("function"), dict):
                tool["function"]["name"] = new_name
            else:
                tool["name"] = new_name
        out_tools.append(tool)
    if not mapping:
        return out_tools, messages

    _RESTORE.set({v: k for k, v in mapping.items()})

    out_msgs: list[dict[str, Any]] = []
    for msg in messages:
        calls = msg.get("tool_calls")
        if isinstance(calls, list):
            msg = dict(msg)
            rewritten = []
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and fn.get("name") in mapping:
                    call = json.loads(json.dumps(call))
                    call["function"]["name"] = mapping[fn["name"]]
                rewritten.append(call)
            msg["tool_calls"] = rewritten
        out_msgs.append(msg)
    return out_tools, out_msgs


_RESTORE: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "proxy_opencode_tool_restore", default={}
)


def restore_map() -> dict[str, str]:
    """Reverse of this request's tool renames (``client_bash`` -> ``bash``)."""

    return _RESTORE.get()


__all__ = [
    "INJECT_NAME_POOL",
    "MINIMAL_TOOL_NAMES",
    "MIN_INJECTED_TOOLS",
    "builtin_tools",
    "merge_tools",
    "minimal_schema",
    "rename_collisions",
    "restore_map",
    "tool_names",
]
