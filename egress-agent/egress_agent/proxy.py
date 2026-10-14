"""HTTP CONNECT proxy data plane (P1).

Planned shape (implementation lands in P1 with tests):

* aiohttp ``web`` server serving two things on the single agent port
  (10001/10002): ``GET /healthz`` (see ``health.py``) and HTTP CONNECT for
  every other method/path.
* CONNECT handler: parse ``Host:Port``, open the outbound socket with the
  configured egress binding (see ``egress.py``), reply ``200 Connection
  established``, then pump bytes both ways with ``asyncio`` (two tasks,
  back-pressured by ``drain()``). No payload inspection, no logging of
  targets.
* plain HTTP (non-CONNECT) requests are rejected with 405 — the gateway
  only ever tunnels TLS via CONNECT.

Mature-library rule (see docs/ARCHITECTURE.md): aiohttp owns HTTP parsing;
the only hand-written part is the duplex pump, which no mature library
provides together with source-address binding.
"""

from __future__ import annotations
