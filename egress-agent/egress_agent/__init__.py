"""egress-agent: device-side egress proxy for the proxy_opencode gateway stack.

The 0.7.x architecture moves the whole gateway into one Docker container on
the ECS host; every quota bucket device runs only this agent, an HTTP CONNECT
proxy whose outbound connections leave through that device's own uplink. The
Zen free-tier quota is metered per egress IP, so the gateway's egress pool
sends each upstream request through one agent and gets that device's IP.

Scope of this package (P0 skeleton — see docs/ARCHITECTURE.md):

* ``config``      JSON config loading/validation (P0, real).
* ``cli``         ``run`` / ``status`` subcommands (P0; ``run`` is P1).
* ``logging_setup`` structured logging without payload content (P0, real).
* ``proxy``       HTTP CONNECT listener + asyncio duplex pump (P1).
* ``egress``      outbound source binding helpers (P1).
* ``health``      ``/healthz``: listener state + current egress IP (P1).
* ``rotate``      ONT reboot driver for IP rotation (P1; ``ont`` config P0).

Hard rules (inherited from the repo's AGENTS.md):

* Never log request content, prompts or credentials.
* Real credentials live only on the device (config file mode 600); the repo
  ships placeholders only.
"""

from __future__ import annotations

__version__ = "0.7.0a1"
