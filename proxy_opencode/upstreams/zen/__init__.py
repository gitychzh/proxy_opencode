"""zen-direct: the OpenCode Zen client protocol, split by concern.

Public surface:

* :class:`ZenDirectAdapter` — the ``UpstreamAdapter`` implementation
  (``upstreams.build_adapter`` wires it for ``UPSTREAM_MODE=zen-direct``);
* :func:`opencode_id` / :func:`is_valid_opencode_id` — id minting, used by the
  capture-audit tooling and tests;
* :func:`render_prompt` / :func:`prompt_date` / :func:`default_prompt` — the
  body-gate rendering;
* :func:`retry_after_seconds` — 429 classification input for the rotator.

Modules and their responsibility:

``protocol``  headers + user agent + Retry-After parsing
``ids``       session/request/workspace identifiers
``prompt``    system-prompt asset and per-request rendering
``tools``     builtin tool-schema injection (shape gate)
``body``      request composition
``aggregate`` SSE -> single completion JSON
``errors``    free-tier 403 rewrite
``client``    adapter orchestration (failover, transport, lease release)

Protocol authority: AGENTS.md 硬约束 1.
"""

from __future__ import annotations

from .body import build_body, clean_messages
from .client import ZenDirectAdapter
from .errors import is_free_tier_rejection
from .ids import is_valid_opencode_id, opencode_id, project_hash
from .prompt import default_prompt, prompt_date, render_prompt
from .protocol import build_headers, retry_after_seconds, user_agent
from .tools import builtin_tools, merge_tools

# Test/tooling compatibility: the historical module exposed these names with a
# leading underscore. Kept as aliases so call sites read naturally against the
# new split without a second implementation.
_default_prompt = default_prompt
_render_prompt = render_prompt
_prompt_date = prompt_date
_retry_after_seconds = retry_after_seconds

__all__ = [
    "ZenDirectAdapter",
    "build_body",
    "build_headers",
    "builtin_tools",
    "clean_messages",
    "default_prompt",
    "is_free_tier_rejection",
    "is_valid_opencode_id",
    "merge_tools",
    "opencode_id",
    "project_hash",
    "prompt_date",
    "render_prompt",
    "retry_after_seconds",
    "user_agent",
]
