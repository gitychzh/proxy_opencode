"""ONT reboot driver for IP rotation (P1; wired into the gateway in P5).

Driver contract::

    restart(config: OntConfig) -> RotationResult
    wait_back(config: OntConfig, timeout_s: float) -> bool

``kind`` selects the driver. First driver is ``"huawei"`` — the pure-HTTP
four-step flow already proven in production on this hardware
(see docs/ARCHITECTURE.md section "ONT rotation"):

1. ``GET {url}/asp/GetRandCount.asp`` (source-bound to the configured
   interface; strip the UTF-8 BOM with ``sed``-equivalent bytes handling —
   ``str.removeprefix`` on the decoded text, never ``tr``).
2. ``POST {url}/login.cgi`` with ``UserName``, base64 ``PassWord``,
   ``x.X_HW_Token``. Success marker: HTTP 200 and body containing
   ``Waiting`` (do NOT rely on a ``sid`` cookie).
3. ``GET {url}/html/ssmp/devmanage/cmccdevicereset.asp`` → extract the
   ``onttoken`` (40+ hex chars).
4. ``POST {url}/set.cgi?x=InternetGatewayDevice.X_HW_DEBUG.SMP.DM.ResetBoard``
   with the token. ``HTTP 000`` (connection dropped) means the ONT accepted
   the reboot.

Recovery: poll the ONT web root until it answers (~80 s), then wait for
PPPoE redial (~30-40 s) before reporting success. The caller (gateway
pool) keeps the egress cordoned until ``/healthz`` reports a NEW egress IP.

Credential hygiene: the ONT password comes from the device config file
(mode 600); it is never logged and never leaves the device — the gateway
triggers rotation through this agent's control endpoint.
"""

from __future__ import annotations
