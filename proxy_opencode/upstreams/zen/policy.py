"""What this model accepts, applied to the outgoing body.

The gateway mirrors the opencode client, so the request it sends upstream must
carry the same *kind* of parameters the CLI would send — no more (a knob the
CLI never exposes must not be forwarded as if it worked) and no less (the
output budget and the context ceiling the CLI computes must exist here too).

Three rules, each traceable to the source tree:

* **reasoning controls** — a model publishes ``capabilities.reasoning.effort``
  only when the user may choose a strength. ``big-pickle`` publishes none
  (models.dev snapshot, 2026-10-27), so the CLI renders no selector and sends
  no control: the model reasons at its built-in maximum. Forwarding a client's
  ``reasoning_effort`` would fake a control that does not exist.
* **output budget** — ``session/model-request.ts`` ``outputLimit``: the smaller
  of the catalogue output limit and the room left in the context window.
* **context ceiling** — ``session/compaction.ts`` ``calculateCeiling``: the
  client compacts below ``input - max(15%, 16k)``. A prompt past the input
  limit is refused here (``ContextOverflow``) instead of being sent to a
  provider that would refuse it more slowly and less legibly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...errors import ContextOverflow
from ...model_profile import (
    ModelProfile,
    estimate_request,
    output_budget,
)

# Fields a client uses to ask for a thinking strength. Names differ per
# protocol (OpenAI flat, Anthropic budgeted block, vendor aliases).
CONTROL_FIELDS = (
    "reasoning_effort",
    "thinking",
    "reasoning",
    "include_reasoning",
    "budget_tokens",
)

REASONING_POLICIES = ("max", "client", "off")
OUTPUT_BUDGET_MODES = ("mirror", "clamp", "off")


@dataclass(frozen=True)
class Applied:
    """What the policy did to one outgoing body (for logs and tests)."""

    estimated_input: int = 0
    dropped: tuple[str, ...] = ()
    max_tokens: int | None = None
    clamped: bool = False
    context_ceiling: int = 0


def apply_request_policy(
    body: dict[str, Any],
    *,
    profile: ModelProfile | None,
    reasoning_policy: str = "max",
    context_guard: bool = True,
    input_limit_override: int = 0,
    output_budget_mode: str = "mirror",
) -> Applied:
    """Rewrite one composed upstream body in place; return what changed.

    ``profile is None`` (an upstream id we have no published parameters for)
    means no enforcement at all: inventing limits for an unknown model is how
    a gateway starts refusing valid traffic.
    """

    if reasoning_policy not in REASONING_POLICIES:
        raise ValueError(
            f"unknown reasoning policy {reasoning_policy!r}; "
            f"expected one of {REASONING_POLICIES}"
        )
    if output_budget_mode not in OUTPUT_BUDGET_MODES:
        raise ValueError(
            f"unknown output budget mode {output_budget_mode!r}; "
            f"expected one of {OUTPUT_BUDGET_MODES}"
        )

    dropped = _apply_reasoning_policy(body, profile, reasoning_policy)
    if profile is None:
        return Applied(dropped=dropped)

    estimated = estimate_request(
        body.get("messages") or [], body.get("tools") or ()
    )
    limit = input_limit_override or profile.limits.window()
    if context_guard and limit > 0 and estimated > limit:
        raise ContextOverflow(
            model=profile.id,
            estimated_tokens=estimated,
            input_limit=limit,
            context_limit=profile.limits.context or limit,
            compaction_ceiling=profile.ceiling(),
        )

    max_tokens, clamped = _apply_output_budget(body, profile, estimated, output_budget_mode)
    return Applied(
        estimated_input=estimated,
        dropped=dropped,
        max_tokens=max_tokens,
        clamped=clamped,
        context_ceiling=profile.ceiling(),
    )


# --------------------------------------------------------------- reasoning


def _apply_reasoning_policy(
    body: dict[str, Any],
    profile: ModelProfile | None,
    policy: str,
) -> tuple[str, ...]:
    """Drop thinking controls this model cannot honour; return their names."""

    declared = frozenset(profile.capabilities.efforts) if profile else frozenset()
    dropped: list[str] = []
    for field in CONTROL_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if policy == "client":
            continue
        keep = (
            policy == "max"
            and declared
            and field == "reasoning_effort"
            and isinstance(value, str)
            and value.strip().lower() in declared
        )
        if keep:
            continue
        body.pop(field, None)
        dropped.append(field)
    return tuple(dropped)


# ------------------------------------------------------------ output budget


def _coerce_output_int(value: Any) -> int | None:
    """Normalise a client-supplied output budget to ``int``.

    ``None`` means uninterpretable (bool, non-numeric string, container…).
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _apply_output_budget(
    body: dict[str, Any],
    profile: ModelProfile,
    estimated: int,
    mode: str,
) -> tuple[int | None, bool]:
    """Fit ``max_tokens`` / ``max_completion_tokens`` to the room left."""

    if mode == "off":
        return None, False
    budget = output_budget(profile.limits, estimated)
    # The newer OpenAI spelling wins when both are present (it is the one the
    # upstream honours), mirroring how the CLI picks its field.
    field = (
        "max_completion_tokens"
        if "max_completion_tokens" in body
        else "max_tokens"
    )
    current = body.get(field)
    if current is None:
        if mode == "clamp":
            return None, False
        body[field] = budget
        return budget, False
    current = _coerce_output_int(current)
    if current is None:
        # An uninterpretable budget ('"8000"', true, …) would be rejected
        # upstream, and the Applied report must match what actually goes
        # out — replace it with the budget rather than passing it through.
        body[field] = budget
        return budget, True
    if current <= budget:
        # Write back so coerced values ('8000' -> 8000) reach the upstream
        # as the number it expects; a plain int is rewritten unchanged.
        body[field] = current
        return current, False
    body[field] = budget
    return budget, True


__all__ = [
    "CONTROL_FIELDS",
    "OUTPUT_BUDGET_MODES",
    "REASONING_POLICIES",
    "Applied",
    "apply_request_policy",
]
