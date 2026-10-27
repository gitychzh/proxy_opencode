"""opencode-compatible identifier minting (session / request / workspace ids).

The upstream's shape gate judges these ids structurally, so the encoding is
protocol fact, not cosmetics:

* 26 chars = 12 hex chars of ``(ms << 12) + counter`` truncated to the low 48
  bits, followed by 14 random chars from ``[0-9A-Za-z]`` (byte % 62);
* ``ses_`` ids must be **descending** (``~value``) since opencode 1.19.0 — an
  ascending session id is rejected with ``FreeTierError``;
* ``msg_`` ids stay ascending, matching the genuine client.

Authority: AGENTS.md 硬约束 1 (session 编码).
"""

from __future__ import annotations

import hashlib
import secrets
import time

# Byte % 62 over the 62-char alphabet, exactly like the client's
# `Identifier.create()`.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_RANDOM_CHARS = 14
_HEX_CHARS = 12


def opencode_id(prefix: str, *, descending: bool = False) -> str:
    """Generate an identifier exactly like opencode's ``Identifier.create()``.

    Gate note (2026-10-13, live-verified): the free tier rejects a session id
    that is ascending-encoded OR whose counter reaches 64 within the same
    millisecond with ``FreeTierError``. Counter 1 is what a real client emits
    for the first id in a millisecond, so always mint with counter 1.
    """

    now_ms = time.time_ns() // 1_000_000
    current = now_ms * 0x1000 + 1
    value = (~current) & 0xFFFFFFFFFFFF if descending else current & 0xFFFFFFFFFFFF
    time_part = format(value, "012x")
    rand = "".join(_ALPHABET[b % 62] for b in secrets.token_bytes(_RANDOM_CHARS))
    return prefix + time_part + rand


def is_valid_opencode_id(value: str, prefix: str) -> bool:
    """Structural check used by tests and the capture-audit tooling."""

    body = value[len(prefix):]
    if len(body) != _HEX_CHARS + _RANDOM_CHARS:
        return False
    try:
        int(body[:_HEX_CHARS], 16)
    except ValueError:
        return False
    return all(c.isalnum() for c in body[_HEX_CHARS:])


def project_hash() -> str:
    """Fixed 40-hex workspace hash for ``x-opencode-project``.

    The genuine client derives it from the workspace directory (sha1). The
    gateway has no single workspace, so a backend-stable constant keeps the
    header byte-stable without pretending to be one real directory.
    """

    return hashlib.sha1(b"opencode-gateway-workspace").hexdigest()


__all__ = ["is_valid_opencode_id", "opencode_id", "project_hash"]
