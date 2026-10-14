"""HTTP CONNECT proxy data plane — Windows build (P6 placeholder).

The Python package is cross-platform; the Windows deployment differs only
in packaging (a scheduled task or `pythonw` service wrapper instead of
systemd) and in egress binding (`socket.bind` only — interface selection
stays with the OS routing table). Implementation lands with the Windows
bucket (P6); nothing in `egress_agent/` is Windows-hostile, so this file
stays documentation-only.
"""

from __future__ import annotations
