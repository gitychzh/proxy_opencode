"""Upstream error shapes specific to the anonymous free tier.

Two rewrites live here, both about not letting an *egress-level* verdict reach
the client wearing the wrong identity:

* a 403 ``FreeTierError`` is the gateway's problem (shape gate or a flagged
  egress IP), never the caller's — relaying it makes clients blame their
  credentials and stop retrying;
* the answer is therefore a retryable ``503 egress_unavailable``.
"""

from __future__ import annotations

from typing import Any

FREE_TIER_ERROR = "FreeTierError"

EGRESS_UNAVAILABLE_MESSAGE = (
    "Upstream free-tier gate rejected the gateway's egress IP "
    "(403 FreeTierError): an egress-level restriction, not a problem with "
    "this request or your credentials. Try again later."
)


def is_free_tier_rejection(status: Any, content: Any) -> bool:
    """True when an error body is the free tier's gate rejection.

    Kept in the egress-failure path (cordon + failover) as well: the shape
    gate and the quota gate cannot be told apart from one answer alone, and a
    spurious cordon on a shape fault costs almost nothing (the rotator lands a
    fresh IP and the corrected shape re-opens the bucket).
    """

    if status != 403 or not isinstance(content, dict):
        return False
    err = content.get("error")
    return isinstance(err, dict) and err.get("type") == FREE_TIER_ERROR


def egress_unavailable_result() -> dict[str, Any]:
    """Shape a final free-tier 403 as a retryable 503 egress outage."""

    return {
        "__status__": 503,
        "content": {
            "error": {
                "message": EGRESS_UNAVAILABLE_MESSAGE,
                "type": "api_error",
                "param": None,
                "code": "egress_unavailable",
            }
        },
    }


def quota_exhausted_result(blackout_s: float) -> dict[str, Any]:
    """Shape "the whole pool is out of quota" without touching the upstream.

    Measured 2026-10-29 02:00 CST: all four egress IPs AND the ECS's own IP
    answered ``FreeUsageLimitError`` at the same time, and a brand-new ESP32
    egress IP (``111.55.72.119`` -> ``111.55.74.1``, MiFi reboot) was rejected
    too. The free tier's daily allowance is therefore **account-scoped**, not
    per-IP: a fresh IP buys no quota, only a fresh rate-limit bucket. Once
    every egress is benched on a quota verdict, spending an upstream round
    trip per bucket per request buys nothing — it only delays the fallback
    chain by one upstream RTT each (measured 7.25s for a 16-token answer that
    the backup could have served immediately).
    """

    return {
        "__status__": 429,
        "__quota_blackout_s__": blackout_s,
        "content": {
            "error": {
                "message": (
                    "The free-tier daily allowance for this account is used "
                    "up; every egress is benched until it resets "
                    f"({blackout_s / 3600.0:.1f}h)."
                ),
                "type": "insufficient_quota",
                "param": None,
                "code": "free_quota_exhausted",
            }
        },
    }


__all__ = [
    "EGRESS_UNAVAILABLE_MESSAGE",
    "FREE_TIER_ERROR",
    "egress_unavailable_result",
    "is_free_tier_rejection",
    "quota_exhausted_result",
]
