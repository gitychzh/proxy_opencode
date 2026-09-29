"""Auth package: gateway/admin bearer auth + dynamic key store."""

from __future__ import annotations

from .core import build_admin_dependency, build_auth_dependency, classify_key
from .keystore import KeyRecord, KeyStore

__all__ = [
    "build_admin_dependency",
    "build_auth_dependency",
    "classify_key",
    "KeyRecord",
    "KeyStore",
]
