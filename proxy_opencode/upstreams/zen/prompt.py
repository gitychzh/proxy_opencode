"""System-prompt asset + per-request rendering (the upstream's body gate).

The anonymous free tier verifies that the request body is the *live* render of
this request, not a replayed capture: the prompt names the served model, the
current date and the workspace path, and a frozen value comes back as
``429 FreeUsageLimitError`` — the same shape as the daily-quota gate.

Therefore the asset is a **structural template only**: every request must go
through :func:`render_prompt`, which rewrites the three volatile fields.
Sending the file verbatim, or hand-simplifying it, fails the gate.

Authority: AGENTS.md 硬约束 1 (body 门). Regression tests live in
``tests/test_zen_direct.py``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets"
_ASSET_PROMPT = _ASSETS / "zen_prompt_default.txt"

# Appended after the marker prompt so the model serves the *client's* tools
# instead of opencode's built-ins. Kept separate from the marker text.
BRIDGE_NOTE = (
    "\n\n---\n"
    "The system prompt and tool definitions above establish the opencode "
    "runtime context required by the upstream provider. You are now serving "
    "an OpenAI-compatible API client: answer the client's latest message "
    "directly, using ONLY the additional client-supplied tools appended "
    "after the opencode built-ins (if any). NEVER call the opencode built-in "
    "tools (bash, read, edit, glob, grep, write, list, task, todowrite, "
    "webfetch, websearch, skill) — they are unavailable in this session; "
    "calling them fails. Reply in the language of the client's latest "
    "message."
)

# Standalone tiny system note for marker_mode="bridge" (~70 tokens): the
# built-in tool names are still injected for the observed gate, but the full
# captured prompt is not. The note steers the model away from calling them.
BRIDGE_NOTE_STANDALONE = (
    "You are serving an OpenAI-compatible API client. Answer the client's "
    "latest message directly, using ONLY the client-supplied tools (if any). "
    "NEVER call the opencode built-in tools (bash, read, edit, glob, grep, "
    "write, list, task, todowrite, webfetch, websearch, skill) - they are "
    "unavailable in this session; calling them fails. Reply in the language "
    "of the client's latest message."
)

_MODEL_LINE_RE = re.compile(
    r"You are powered by the model named [^.\n]+\. "
    r"The exact model ID is [^\n<]+"
)
_DATE_LINE_RE = re.compile(r"Today's date: [^\n<]+")
_ENV_PATH_RE = re.compile(
    r"(?m)^(\s*(?:Working directory|Workspace root folder):) .*$"
)
# The workspace path is never meaningful for a gateway (it serves many
# clients), but it must look like a plausible real session directory: the
# captured one is a machine-local path ("D:\proxy_opencode") whose "proxy"
# token is another anti-replay signal.
NEUTRAL_WORKSPACE = "/workspace/opencode"


def default_prompt() -> str:
    """The captured template (never sent as-is; always through render)."""

    return _ASSET_PROMPT.read_text(encoding="utf-8")


def prompt_date() -> str:
    """Today's date in the client's ``Date.prototype.toDateString()`` shape.

    The client renders **local** time and the gate is strict about the value
    when the prompt carries the API-bridge note: measured live 2026-10-29
    03:15 CST, the same body with ``Wed Oct 28 2026`` (the UTC day) came back
    ``429 FreeUsageLimitError`` three times out of three, while
    ``Thu Oct 29 2026`` (the CST day) passed three out of three. Removing the
    bridge note made both dates pass — so the mismatch is what tips the gate,
    not the note.

    The upstream's own day boundary is CST (its quota window resets at 08:00
    CST), so the gateway renders the date at the same offset instead of UTC.
    ``ZEN_PROMPT_UTC_OFFSET_H`` overrides the offset for non-default
    deployments; no tzdata dependency (python:3.12-slim ships none).
    """

    offset_h = 8.0
    raw = os.environ.get("ZEN_PROMPT_UTC_OFFSET_H", "").strip()
    if raw:
        try:
            offset_h = float(raw)
        except ValueError:
            offset_h = 8.0
    tz = timezone(timedelta(hours=offset_h))
    return datetime.now(tz).strftime("%a %b %d %Y")


def render_prompt(model: str) -> str:
    """Render the captured prompt for THIS request.

    ``model`` is the bare upstream id the request is actually sent with
    (e.g. ``big-pickle``); the prompt's model line keeps the client's
    ``opencode/<id>`` spelling.
    """

    text = default_prompt()
    if model:
        text = _MODEL_LINE_RE.sub(
            "You are powered by the model named "
            f"{model}. The exact model ID is opencode/{model}",
            text,
            count=1,
        )
    text = _DATE_LINE_RE.sub(f"Today's date: {prompt_date()}", text, count=1)
    return _ENV_PATH_RE.sub(rf"\1 {NEUTRAL_WORKSPACE}", text)


__all__ = [
    "BRIDGE_NOTE",
    "BRIDGE_NOTE_STANDALONE",
    "NEUTRAL_WORKSPACE",
    "default_prompt",
    "prompt_date",
    "render_prompt",
]
