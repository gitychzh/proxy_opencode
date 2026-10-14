"""Egress-pool building blocks for the 0.7.x architecture.

The gateway no longer runs on the bucket devices: it lives in one Docker
container on the ECS host and reaches every quota bucket through that
device's ``egress-agent`` proxy (``http://<tailscale-ip>:10001``). The Zen
free tier meters quota per egress IP, so the pool must:

* pick an egress per upstream request (least in-flight, fail fast),
* cordon an egress on transport failure / hard 429 and probe it back,
* count completed requests per egress against a daily window (08:00
  Asia/Shanghai — UTC 00:00, when the upstream quota resets) and trigger an
  ONT reboot through the agent when a bucket hits the configured limit,
* report status for ``/admin/egresses``.

Submodules and the phases that fill them (see docs/ARCHITECTURE.md):

* ``pool``     — egress registry, selection, cordon, health probes (P2).
* ``counters`` — daily-window counters + rotation trigger (P5).
* ``catalog``  — Zen free-model discovery + public alias policy (P3).
* ``updater``  — upstream opencode release watcher (P4).
* ``rotate``   — gateway-side rotation orchestration (calls each agent's
  control endpoint; the ONT driver itself lives in egress-agent) (P5).

Everything in this package must keep the repo's log red line: never log
prompts, message content or credentials — egress names, IPs, counters and
transition timestamps are fine.
"""

from __future__ import annotations
