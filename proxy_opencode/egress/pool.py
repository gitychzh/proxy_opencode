"""Egress pool: registry, selection, cordon and health (P2).

Planned contract::

    pool = EgressPool.from_settings(settings)      # per-name httpx client
    async with pool.acquire("opc2-wifi") as client: ...
    pool.snapshot() -> list[EgressStatus]           # for /admin/egresses

Design notes:

* One ``httpx.AsyncClient`` per egress with ``proxy=<agent url>`` and
  ``trust_env=False`` (the ECS fork of the balancer already learned that
  lesson). Selection is least-in-flight among healthy egresses; when every
  egress is saturated the request still proceeds (queue upstream instead of
  inventing a 502).
* Cordon rules mirror the proven balancer semantics: a cordoned egress is
  skipped by selection; three consecutive transport failures cordon it for
  a cooldown; a hard upstream 429 (``FreeUsageLimitError``) cordons until
  the daily window resets or a rotation succeeds.
* Health probes hit each agent's ``GET /healthz`` (see egress-agent) on a
  slow loop; ``egress_ip`` from the probe is the authoritative "what does
  the upstream see" record and feeds the IP-change check after rotations.
"""

from __future__ import annotations
