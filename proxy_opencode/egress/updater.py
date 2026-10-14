"""Upstream opencode release watcher (P4).

Every day at 02:00 Asia/Shanghai the watcher asks the GitHub API for new
releases of ``anomalyco/opencode`` — the official repo (213k+ stars; the
``sst`` organisation renamed to ``anomalyco`` and old links redirect) —
**including prereleases**, then classifies what changed against the
protocol facts this gateway depends on (session-id encoding, tool-name
gate, streaming requirement, headers, prompt asset):

* Tier 1 (auto-apply): the bundled system-prompt asset and the pinned
  user-agent version string.
* Tier 2 (staged): identifier/algorithm constants — applied only after the
  full test suite plus a live smoke request through one egress pass;
  automatic rollback otherwise.
* Tier 3 (report only): structural changes — surfaced on ``/healthz`` and
  the admin endpoint for a human decision.

The watcher never rewrites code by itself; it produces a protocol diff
report and, for tiers 1-2, applies the curated changes that the repo
declares for that release.
"""

from __future__ import annotations
