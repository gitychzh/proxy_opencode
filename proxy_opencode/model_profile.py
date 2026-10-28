"""Model parameters, mirrored from the opencode client that the gateway fronts.

The gateway is a *stand-in for the provider*, so its numbers must be the
provider's numbers. Everything here is transcribed from the opencode source
tree (``sst/opencode`` @ ``dev``, 2026-10-27):

* ``packages/core/src/models-dev/snapshot.txt`` — the catalogue snapshot the
  CLI itself ships (provider ``opencode``). Limits, capability flags and the
  declared reasoning efforts come from there; ``big-pickle`` is copied
  verbatim: ``limit {context 200000, input 160000, output 32000}`` and
  ``capabilities.reasoning = {supported: true}`` — with **no** ``effort``
  list, i.e. the CLI offers no thinking-strength control for this model.
* ``packages/core/src/util/token.ts`` — ``CHARS_PER_TOKEN = 4``: the prompt
  estimator both sides use.
* ``packages/core/src/session/compaction.ts`` — auto-compaction ceiling:
  ``(limit.input || limit.context) - max(15% of window, 16k)``; big-pickle
  therefore compacts at ``160000 - 24000 = 136000`` tokens. A provider
  "context too long" rejection also starts a compaction (``reason:
  "overflow"``), which is why :class:`~proxy_opencode.errors.ContextOverflow`
  must keep the OpenAI ``context_length_exceeded`` shape.
* ``packages/core/src/session/model-request.ts`` — output budget:
  ``min(limit.output, max(1024, context - measured - ceil(estimate*1.15)))``
  with ``OUTPUT_TOKEN_MAX`` 256k, ``OUTPUT_TOKEN_FALLBACK`` 32k.

Keeping this table tiny and frozen is deliberate: a model whose parameters we
invent is a model whose limits clients cannot reason about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# opencode: packages/core/src/util/token.ts
CHARS_PER_TOKEN = 4
# opencode: session/compaction.ts — reserve below the input limit before compacting.
RESERVE_SHARE = 0.15
RESERVE_MIN = 16_000
# opencode: session/model-request.ts — output budget bounds.
OUTPUT_TOKEN_MAX = 256_000
OUTPUT_TOKEN_MIN = 1_024
OUTPUT_TOKEN_FALLBACK = 32_000
ESTIMATE_ERROR = 0.15
# opencode: compaction.ts UNKNOWN_WINDOW — assumed window when the model is unknown.
UNKNOWN_WINDOW = 200_000


@dataclass(frozen=True)
class Limits:
    """Token limits exactly as the upstream catalogue declares them."""

    context: int = 0
    input: int = 0
    output: int = 0

    def window(self) -> int:
        """The window used for budgeting: the input limit wins (opencode)."""

        return self.input or self.context


@dataclass(frozen=True)
class Capabilities:
    """Capability flags the CLI reads from the catalogue."""

    tools: bool = True
    reasoning: bool = False
    # Declared effort levels. Empty == the model reasons, but the user cannot
    # choose a strength (opencode renders no selector for it).
    efforts: tuple[str, ...] = ()
    structured_output: bool = True
    temperature: bool = True
    attachment: bool = False
    # Name of the delta field carrying thinking text (interleaved reasoning).
    reasoning_field: str = "reasoning_content"


@dataclass(frozen=True)
class ModelProfile:
    """One upstream model's published parameters."""

    id: str
    limits: Limits
    capabilities: Capabilities
    source: str = "models.dev/opencode@2026-10-27"

    def ceiling(self) -> int:
        """First prompt size at which opencode compacts this model."""

        return compaction_ceiling(self.limits)

    def as_metadata(self) -> dict[str, Any]:
        """Catalogue-shaped view for ``/v1/models_cus?format=json``."""

        cap = self.capabilities
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "opencode",
            "limit": {
                "context": self.limits.context,
                "input": self.limits.input,
                "output": self.limits.output,
            },
            "capabilities": {
                "tools": cap.tools,
                "reasoning": cap.reasoning,
                "reasoning_efforts": list(cap.efforts),
                "structured_output": cap.structured_output,
                "temperature": cap.temperature,
                "attachment": cap.attachment,
            },
            "reasoning_field": cap.reasoning_field,
            "compaction_ceiling": self.ceiling(),
            "source": self.source,
        }


# --------------------------------------------------------------------------
# The relayed models. Values are the upstream catalogue's own numbers; a model
# missing here is served with no enforced limits rather than with invented
# ones (see profile()).
# --------------------------------------------------------------------------
_PROFILES: dict[str, ModelProfile] = {
    # Verified live 2026-10-27: reasoning supported with no effort options —
    # the CLI exposes no thinking-strength switch for it, so neither do we.
    #
    # LIMITS RE-MEASURED LIVE 2026-10-29 — the shipped catalogue is stale:
    # models.dev/snapshot.txt says {context 200000, input 160000}, but the
    # upstream itself answers with its own ceiling when pushed past it:
    #   "Input token count (294840) exceeds the model's maximum context
    #    length of (262139), no tokens left for generation"
    # Measured by sweeping prompts through a canary with the local guard
    # disabled (each row = upstream-reported prompt_tokens):
    #   57,240 → 200   92,440 → 200   104,760 → 200  118,840 → 200
    #   132,040 → 200  149,640 → 200  162,840 → 200  198,040 → 200
    #   203,320 → 200  233,240 → 200  294,840 → 400 (262,139)
    # So the provider grants a 262,139-token window, and the binding rule is
    # prompt + max_tokens ≤ 262,139 ("no tokens left for generation").
    # `input` is set 1,139 below the window so the guard can never leave a
    # prompt that the 1,024-token output floor would push back over it.
    "big-pickle": ModelProfile(
        id="big-pickle",
        limits=Limits(context=262_139, input=261_000, output=32_000),
        capabilities=Capabilities(
            tools=True,
            reasoning=True,
            efforts=(),
            structured_output=True,
            temperature=True,
            attachment=False,
        ),
    ),
    "deepseek-v4.1-flash-free": ModelProfile(
        id="deepseek-v4.1-flash-free",
        limits=Limits(context=200_000, output=32_000),
        capabilities=Capabilities(
            reasoning=True, efforts=("low", "high", "max"), attachment=False
        ),
    ),
    "glm-5.3-flash-free": ModelProfile(
        id="glm-5.3-flash-free",
        limits=Limits(context=200_000, output=32_000),
        capabilities=Capabilities(
            reasoning=True, efforts=("low", "high", "max"), attachment=False
        ),
    ),
    "muse-spark-1.3-contributor-free": ModelProfile(
        id="muse-spark-1.3-contributor-free",
        limits=Limits(context=1_048_576, output=131_072),
        capabilities=Capabilities(
            reasoning=True,
            efforts=("minimal", "low", "medium", "high", "xhigh"),
            attachment=False,
        ),
    ),
}


def profile(model_id: Any) -> ModelProfile | None:
    """Look up a profile by upstream id, tolerating a ``provider/`` prefix."""

    raw = str(model_id or "").strip()
    if not raw:
        return None
    if raw in _PROFILES:
        return _PROFILES[raw]
    _, sep, tail = raw.partition("/")
    if sep and tail in _PROFILES:
        return _PROFILES[tail]
    return None


def profiles() -> Mapping[str, ModelProfile]:
    """Read-only view of the table (tests and catalogue rendering)."""

    return dict(_PROFILES)


# ---------------------------------------------------------------- estimators


def estimate_text(text: Any) -> int:
    """opencode's estimator: ``round(len(text) / 4)``, never negative."""

    if not isinstance(text, str) or not text:
        return 0
    return max(0, round(len(text) / CHARS_PER_TOKEN))


def _text_of(content: Any) -> str:
    """Flatten an OpenAI message ``content`` (str or part list) to text."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text") or part.get("content")
                parts.append(value if isinstance(value, str) else "")
        return "".join(parts)
    return ""


def estimate_message(message: Mapping[str, Any]) -> int:
    """Prompt tokens one message costs, including tool calls and results."""

    total = estimate_text(_text_of(message.get("content")))
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict):
                continue
            total += estimate_text(fn.get("name"))
            total += estimate_text(fn.get("arguments"))
    return total


def estimate_request(
    messages: Iterable[Any],
    tools: Iterable[Any] | None = None,
) -> int:
    """Prompt tokens for a whole request (opencode's ``estimateRequest``)."""

    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += estimate_message(message)
    for tool in tools or ():
        if isinstance(tool, dict):
            fn = tool.get("function")
            payload = fn if isinstance(fn, dict) else tool
            total += estimate_text(payload.get("name"))
            total += estimate_text(payload.get("description"))
            params = payload.get("parameters") or payload.get("input_schema")
            if params is not None:
                total += estimate_text(_compact_json(params))
    return total


def _compact_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------------- budgets


def compaction_ceiling(limits: Limits) -> int:
    """Prompt size at which opencode compacts (``calculateCeiling``).

    ``(input || context) - max(15% of the window, 16k)``; for big-pickle that
    is ``160000 - 24000 = 136000``. ``0`` when the model declares no window.
    """

    window = limits.window()
    if window <= 0:
        return 0
    floor_reserve = RESERVE_MIN if window >= 2 * RESERVE_MIN else 0
    reserve = max(int(window * RESERVE_SHARE), floor_reserve)
    return max(0, window - reserve)


def output_budget(limits: Limits, estimated_input: int = 0) -> int:
    """``max_tokens`` opencode would send for this prompt (``outputLimit``).

    ``min(limit.output, max(1024, context - measured - ceil(estimate * 1.15)))``
    with ``measured`` counted as zero (nothing is cached server-side here, and
    the estimate is deliberately pessimistic).
    """

    requested = min(
        limits.output if limits.output > 0 else OUTPUT_TOKEN_FALLBACK,
        OUTPUT_TOKEN_MAX,
    )
    if limits.context <= 0:
        return requested
    room = limits.context - estimated_input - math.ceil(
        estimated_input * (1 + ESTIMATE_ERROR)
    )
    return min(requested, max(OUTPUT_TOKEN_MIN, room))


__all__ = [
    "CHARS_PER_TOKEN",
    "OUTPUT_TOKEN_FALLBACK",
    "OUTPUT_TOKEN_MAX",
    "OUTPUT_TOKEN_MIN",
    "UNKNOWN_WINDOW",
    "Capabilities",
    "Limits",
    "ModelProfile",
    "compaction_ceiling",
    "estimate_message",
    "estimate_request",
    "estimate_text",
    "output_budget",
    "profile",
    "profiles",
]
