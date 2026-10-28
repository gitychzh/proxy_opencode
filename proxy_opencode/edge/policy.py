"""Route/failover decisions and header surgery — pure, so they stay testable.

Nothing here performs I/O: the app module owns connections, this module owns
the judgement calls (when a failure is the gateway's fault rather than the
client's) and the byte-level rewriting that makes a fallback invisible.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

# RFC 7230 §6.1 hop-by-hop headers plus the ones a proxy must not forward.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Dropped from the request in addition to the hop-by-hop set: the framing is
# recomputed by httpx, and the Host must be the upstream's.
_DROP_REQUEST = _HOP_BY_HOP | {"host", "content-length", "accept-encoding", "expect"}

# Dropped from the response: we re-frame (chunked for streams, explicit length
# for buffered replies), so the upstream's framing headers would be wrong.
_DROP_RESPONSE = _HOP_BY_HOP | {"content-length"}

# A 429 whose Retry-After sits past the throttle boundary is the daily-quota
# verdict (see egress/models.py): the primary will keep failing for hours.
HARD_QUOTA_RETRY_AFTER_S = 3600.0

_QUOTA_CODES = frozenset({"free_quota_exhausted", "insufficient_quota"})


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)


def iter_header_pairs(headers: Iterable[Any] | Any) -> list[tuple[str, str]]:
    """Normalise ASGI raw headers / httpx headers into ``(lower_name, value)``.

    ``httpx.Headers`` iterates over *keys*, not pairs, and repeats must survive
    (``set-cookie``), hence the ``multi_items()`` branch before the tuple scan.
    """

    if hasattr(headers, "multi_items"):  # httpx.Headers
        return [(_as_text(k).lower(), _as_text(v)) for k, v in headers.multi_items()]
    if hasattr(headers, "items") and not isinstance(headers, (list, tuple)):
        return [(_as_text(k).lower(), _as_text(v)) for k, v in headers.items()]

    out: list[tuple[str, str]] = []
    for item in headers:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            out.append((_as_text(item[0]).lower(), _as_text(item[1])))
        elif isinstance(item, str):
            # A bare key leaking in (e.g. a plain Mapping iterator) is ignored
            # rather than guessed at: a header without a value is meaningless.
            continue
    return out


def filter_request_headers(headers: Iterable[Any]) -> list[tuple[str, str]]:
    """Headers to send upstream, minus framing and minus client compression.

    ``accept-encoding`` is re-declared as ``identity``: the edge forwards the
    response bytes raw (``aiter_raw``) so that SSE latency is untouched, which
    means a compressed body could not be inspected for a quota verdict.
    """

    kept = [(k, v) for k, v in iter_header_pairs(headers) if k not in _DROP_REQUEST]
    kept.append(("accept-encoding", "identity"))
    return kept


def filter_response_headers(headers: Iterable[Any]) -> list[tuple[str, str]]:
    """Response headers safe to relay after re-framing."""

    return [(k, v) for k, v in iter_header_pairs(headers) if k not in _DROP_RESPONSE]


def header_value(headers: Iterable[Any], name: str) -> str:
    name = name.lower()
    for key, value in iter_header_pairs(headers):
        if key == name:
            return value
    return ""


def retry_after_seconds(headers: Iterable[Any]) -> float | None:
    raw = header_value(headers, "retry-after").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def quota_verdict(status: int, headers: Iterable[Any], body: bytes) -> bool:
    """True when a 429 is a daily-allowance verdict, not a short throttle."""

    if status != 429:
        return False
    after = retry_after_seconds(headers)
    if after is not None and after > HARD_QUOTA_RETRY_AFTER_S:
        return True
    if not body:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return False
    return str(err.get("code") or "") in _QUOTA_CODES or str(err.get("type") or "") in _QUOTA_CODES


def is_fallback_path(path: str, allowed: frozenset[str]) -> bool:
    """Only endpoints the backup relay can actually speak are fallback-able."""

    return path.rstrip("/") in {p.rstrip("/") for p in allowed}


def rewrite_for_fallback(
    body: bytes, settings: Any
) -> tuple[bytes, str] | None:
    """Point an OpenAI chat body at the backup's model id.

    Returns ``(new_body, original_model)`` or ``None`` when the body is not a
    JSON object (nothing sensible to rewrite, so no fallback happens). The
    original model is echoed back on success so the client still sees the id
    it asked for.
    """

    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    original = payload.get("model")
    original_model = str(original) if isinstance(original, str) else ""
    payload["model"] = settings.fallback_model
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), original_model
    except (TypeError, ValueError):
        return None


def mask_model_in_json(body: bytes, model: str) -> bytes:
    """Echo the client's own model id in a JSON completion.

    The backup answers with ``deepseek-ai/DeepSeek-V4.1-Flash-1017``; agents
    that log or assert on the model id would otherwise see a second provider
    surface. Any parse problem returns the body untouched.
    """

    if not model or not body:
        return body
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict) or "model" not in payload:
        return body
    payload["model"] = model
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def failover_reason(
    status: int,
    headers: Iterable[Any],
    body: bytes,
    *,
    failover_statuses: frozenset[int],
    failover_on_quota: bool,
) -> str | None:
    """Why the primary's answer should be replaced, or ``None`` to relay it.

    Client errors (400/401/404/422 and friends) are answers, not outages: the
    backup would return the same class of error with a different message, which
    is strictly worse for debugging.
    """

    if status in failover_statuses:
        return f"primary_http_{status}"
    if failover_on_quota and quota_verdict(status, headers, body):
        return "primary_quota_exhausted"
    return None


__all__ = [
    "HARD_QUOTA_RETRY_AFTER_S",
    "failover_reason",
    "filter_request_headers",
    "filter_response_headers",
    "header_value",
    "is_fallback_path",
    "iter_header_pairs",
    "mask_model_in_json",
    "quota_verdict",
    "retry_after_seconds",
    "rewrite_for_fallback",
]
