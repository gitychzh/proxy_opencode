"""Gateway-side rotation orchestration (P5).

The gateway owns the trigger (see ``counters``); this module executes it:

1. mark the egress rotating (selection skips it; in-flight requests finish
   or die — hermes' continuation semantics tolerate that);
2. ``POST {agent}/rotate`` (the agent runs the ONT driver and returns the
   previous/new egress IP);
3. poll the agent's ``/healthz`` until the egress is back and the IP is
   verifiably different (an unchanged IP is logged loudly and reported as a
   failed rotation — the ISP sometimes re-issues the same address);
4. clear the cordon, restart the bucket's counter.

All agent calls carry the egress's shared secret; no credential is ever
returned to or stored by the client side.
"""

from __future__ import annotations
