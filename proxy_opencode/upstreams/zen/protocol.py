"""Wire protocol: request headers and user agent.

Byte-shape facts (all verified against a genuine 1.19.4 capture, see
AGENTS.md 硬约束 1):

* ``User-Agent: opencode/<ver> ai-sdk/provider-utils/<pv> runtime/bun/<bv>``
  (the value is not itself sensitive, but keep the mirror);
* ``x-opencode-client: cli``;
* ``x-opencode-project``: the genuine 1.19.5 client sends the literal
  ``global`` when run outside a workspace (verified against a live capture
  2026-11-03); 1.19.4 sent a 40-hex workspace hash. The gateway mirrors the
  current client shape by default (``PROJECT_ANONYMOUS``); a fixed 40-hex
  value would also be a cross-IP trackable identity, which the literal
  ``global`` is not (every workspace-less opencode user shares it).
* ``x-opencode-session`` **and** ``x-opencode-session-id`` carry the SAME
  freshly minted descending session id — the session is single-use upstream,
  so headers must be built inside the per-attempt loop;
* ``x-opencode-request`` (``msg_``) is ascending; no legacy ``x-session-id``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

from .ids import opencode_id

CLIENT_HEADER = "cli"
PROVIDER_UTILS_VERSION = "4.0.23"
ANONYMOUS_BEARER = "Bearer public"
# 1.19.5 wire fact (live capture 2026-11-03): workspace-less clients send
# this literal; it is the safest default because upstream cannot aggregate
# per-client identity on a value every anonymous user shares.
PROJECT_ANONYMOUS = "global"


def user_agent(client_version: str, bun_version: str) -> str:
    return (
        f"opencode/{client_version} ai-sdk/provider-utils/{PROVIDER_UTILS_VERSION} "
        f"runtime/bun/{bun_version}"
    )


def build_headers(
    *,
    client_version: str,
    bun_version: str,
    project_id: str = "",
    api_key: str = "",
    request_id: bool = True,
) -> dict[str, str]:
    """One attempt's protocol headers, in the capture's order.

    A new session id is minted on every call: the upstream treats a session as
    single-use, and replaying a consumed one returns the quota-shaped 429 even
    from a healthy egress IP.
    """

    session = opencode_id("ses_", descending=True)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": user_agent(client_version, bun_version),
        "x-opencode-client": CLIENT_HEADER,
        "x-opencode-project": project_id or PROJECT_ANONYMOUS,
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        # The genuine client sends the well-known anonymous bearer; a
        # configured Zen key replaces it.
        "Authorization": f"Bearer {api_key}" if api_key else ANONYMOUS_BEARER,
    }
    if request_id:
        headers["x-opencode-request"] = opencode_id("msg_")
    return headers


__all__ = [
    "ANONYMOUS_BEARER",
    "PROJECT_ANONYMOUS",
    "build_headers",
    "retry_after_seconds",
    "user_agent",
]


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Parse an upstream 429's Retry-After into seconds.

    RFC 7231 allows integer seconds or an HTTP-date. The value feeds the
    rotation trigger's measured classification: > 3600s means the per-IP daily
    quota is exhausted (short values are soft throttling). Unparseable or
    absent → None ("no classification available").
    """

    if headers is None:
        return None
    raw = headers.get("retry-after")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
