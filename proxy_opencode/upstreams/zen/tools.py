"""Free-tier tool-shape gate: injection of builtin tool schemas.

The gate requires **>= 4 distinct tool names** that belong to opencode's
builtin set, with ``stream: true``; schema and description contents are not
inspected (live ablation 2026-10-24). One renamed/unknown name 403s even amid
eleven real ones.

``ZEN_TOOLS_MODE`` picks the injected set:

* ``minimal`` (default) — four synthesized ~30-token schemas under builtin
  names the client is not already using (~120 prompt tokens);
* ``captured2`` — the ``{bash, read}`` subset of the captured asset;
* ``all`` — the full captured 11-tool list (legacy).

Authority: AGENTS.md 硬约束 1 (形状门).
"""

from __future__ import annotations

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

    if mode == "all":
        injected = [
            t for t in builtins if not (tool_names([t]) & client_names)
        ]
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


__all__ = [
    "INJECT_NAME_POOL",
    "MINIMAL_TOOL_NAMES",
    "MIN_INJECTED_TOOLS",
    "builtin_tools",
    "merge_tools",
    "minimal_schema",
    "tool_names",
]
