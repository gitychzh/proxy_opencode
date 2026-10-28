"""Client-facing edge: transparent pass-through in front of the model gateway.

Why this layer exists (0.8.0, 2026-10-29)
----------------------------------------
The free-tier upstream's shape gate turned out to be so tightly coupled to the
client's own body (builtin tool set, prompt template, local date) that the
gateway needs frequent code changes. Every such change used to mean the only
public port (``10002``) went dark for the length of a rebuild — users' agent
sessions died with a connection error.

``proxy_opencode.edge`` is a second container on the same image that owns
``10002`` and does exactly two things:

* forward every request to the gateway container (``primary``);
* fall back to the ModelScope relay (``proxy_ms``, ``172.17.0.1:10003``,
  ``deepseek-ai/DeepSeek-V4.1-Flash-1017``) when the primary cannot serve —
  container being rebuilt, connection refused, 502/503/504, or a quota
  verdict that the backup can actually answer.

Failover is decided **before the first byte reaches the client**, so a switch
is invisible: no truncated stream, no half-written SSE frame.
"""

from __future__ import annotations

from .app import EdgeApp, create_edge_app
from .config import EdgeSettings, load_edge_settings

__all__ = ["EdgeApp", "EdgeSettings", "create_edge_app", "load_edge_settings"]
