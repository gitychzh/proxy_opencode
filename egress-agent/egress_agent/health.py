"""Health endpoint (P1).

``GET /healthz`` returns JSON (no secrets)::

    {"egress": "opc2-wifi", "ready": true,
     "egress_ip": "117.95.231.70", "egress_ip_at": "2026-10-14T00:00:00+00:00",
     "tunnels_active": 2, "tunnels_total": 1234}

``egress_ip`` is probed from the device itself (``echo_url`` from the
config, bound through the same egress) so the gateway's pool can verify the
actual public IP and detect IP changes after a rotation. The probe result
is cached for a few seconds; a failing probe sets ``ready=false`` but keeps
the listener serving (the gateway decides cordoning).
"""

from __future__ import annotations
