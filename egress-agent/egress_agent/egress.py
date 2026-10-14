"""Outbound source-address binding (P1).

``EgressConfig.bind_source`` pins the source IP of every outbound socket so
the uplink is deterministic regardless of the OS default route:

* Linux: ``socket.bind((bind_source, 0))``; when ``SO_BINDTODEVICE`` is
  available and an interface is configured, bind to it as well.
* Windows (P6): ``socket.bind`` only; interface selection stays with the
  OS route table.

The binding is applied in ``proxy.py`` when opening tunneled connections
and in ``health.py``/``rotate.py`` for their own outbound calls.
"""

from __future__ import annotations
