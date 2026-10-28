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
    """Shape "every account in the pool is out of quota" without an upstream call.

    Bucket model (owner decision 2026-10-29): the four egress buckets are
    treated as four independent accounts. One account running dry only costs
    that bucket — this *request* fails over to the backup chain and the next
    request resumes round-robin on a sibling bucket. Only when **all** buckets
    carry a quota verdict is there nothing left to rotate to; then a blackout
    deadline is worth stamping so the caller can hold the backup for the
    documented reset window instead of re-probing a dead pool every request.
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
