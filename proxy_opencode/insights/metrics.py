"""Metric aggregation: raw records -> one comparable metric block.

Deterministic by construction (nearest-rank percentiles, integer counters), so
the same window always yields the same numbers and a diagnosis can be replayed.

The phase split is the delicate part: ``latency_ms`` lives on the request
record and ``duration_ms`` on the stream record of the SAME turn, so they are
paired by ``request_id`` before subtracting. Subtracting within one line always
sees one of the two fields missing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .. import __version__
from .parse import record_epoch

# Stream end outcomes that mean "the client got a clean, complete answer".
CLEAN_STREAM_REASONS = frozenset({"completed"})

# Default alignment window for the ledger, matching the documented log dump
# (``docker logs zen-gw --since 24h``). The two sources are read with
# different scopes — the ledger is a whole-file ``tail`` — so the events side
# must be cut to the log's window or the report compares a multi-day history
# against one day of traffic.
EVENTS_WINDOW_HOURS = 24.0


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (deterministic, no interpolation).

    ``q`` is clamped to (0, 1]; an empty sample yields None so callers can
    distinguish "no data" from "zero".
    """

    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if q <= 0:
        return ordered[0]
    if q >= 1:
        return ordered[-1]
    rank = max(1, int(-(-len(ordered) * q // 1)))  # ceil(n*q)
    return ordered[min(rank, len(ordered)) - 1]


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _event_name(record: Mapping[str, Any]) -> str:
    name = record.get("event")
    return str(name) if isinstance(name, str) else ""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _newest_epoch(records: Sequence[Mapping[str, Any]]) -> float | None:
    newest: float | None = None
    for record in records:
        epoch = record_epoch(record)
        if epoch is not None and (newest is None or epoch > newest):
            newest = epoch
    return newest


def window_events(
    events: Sequence[Mapping[str, Any]],
    logs: Sequence[Mapping[str, Any]],
    *,
    window_hours: float = EVENTS_WINDOW_HOURS,
    log_tz_offset_s: float = 0.0,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """Cut the ledger to the same window the logs were dumped with.

    The anchor is the newest timestamp *on either side*, not the wall clock:
    a replay of a saved window must yield the same numbers as the live run
    (determinism is the whole point of this module), and the wall clock would
    silently slide every time the report is re-rendered.

    ``log_tz_offset_s`` is the offset to subtract from the log side's parsed
    epoch when the log producer stamps *local* wall time while the ledger
    stamps UTC (measured 2026-10-28: the desktop text log is local, the
    container ledger is UTC — the naive timestamp then lands ``off`` hours in
    the future and a tight window cuts the whole ledger away). Pass the
    producer's UTC offset (``--log-tz local`` on the CLI resolves it).

    Records without a parseable ``ts`` are kept and counted separately: they
    cannot be attributed to a window, and silently dropping them would hide
    a ledger whose timestamps stopped being written.
    """

    log_anchor = _newest_epoch(logs)
    if log_anchor is not None and log_tz_offset_s:
        log_anchor -= float(log_tz_offset_s)
    anchor = log_anchor
    if anchor is None:
        anchor = _newest_epoch(events)
    undated = sum(1 for record in events if record_epoch(record) is None)
    if anchor is None or window_hours <= 0:
        return list(events), {
            "hours": float(window_hours),
            "anchor": None if anchor is None else _iso(anchor),
            "kept": len(events),
            "dropped": 0,
            "undated": undated,
        }
    cutoff = anchor - float(window_hours) * 3600.0
    kept: list[Mapping[str, Any]] = []
    dropped = 0
    for record in events:
        epoch = record_epoch(record)
        if epoch is None or epoch >= cutoff:
            kept.append(record)
        else:
            dropped += 1
    return kept, {
        "hours": float(window_hours),
        "anchor": _iso(anchor),
        "kept": len(kept),
        "dropped": dropped,
        "undated": undated,
    }


def _summarize_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    events_by_name: dict[str, int] = {}
    cordon_reasons: dict[str, int] = {}
    fallback_reasons: dict[str, int] = {}
    fallback_models: dict[str, int] = {}
    probe_by_egress: dict[str, int] = {}
    rotation_ok = 0
    rotation_failed = 0
    ips: dict[str, set[str]] = {}

    for record in events:
        name = _event_name(record)
        if not name:
            continue
        events_by_name[name] = events_by_name.get(name, 0) + 1
        if name == "probe_failed":
            # One row per healthy->unhealthy *transition* (pool.py logs a
            # single line per transition, not per failed probe). A high count
            # therefore means flapping, while a sustained outage shows as 1.
            egress = record.get("egress")
            if isinstance(egress, str) and egress:
                probe_by_egress[egress] = probe_by_egress.get(egress, 0) + 1
        elif name == "cordon":
            reason = str(record.get("cordon_reason") or "unknown")
            core = reason.split(" (", 1)[0].strip() or "unknown"
            cordon_reasons[core] = cordon_reasons.get(core, 0) + 1
        elif name == "fallback_engaged":
            reason = str(record.get("reason") or "unknown")[:60]
            fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
            model = str(record.get("model") or "")
            if model:
                fallback_models[model] = fallback_models.get(model, 0) + 1
        elif name == "rotation_succeeded":
            rotation_ok += 1
        elif name == "rotation_failed":
            rotation_failed += 1
        egress = record.get("egress")
        ip = record.get("current") or record.get("ip_to") or record.get("egress_ip")
        if isinstance(egress, str) and isinstance(ip, str) and ip:
            ips.setdefault(egress, set()).add(ip)

    block = {
        "total": sum(events_by_name.values()),
        "by_name": dict(sorted(events_by_name.items())),
        "cordon_reasons": dict(sorted(cordon_reasons.items())),
        "fallback_incidents": events_by_name.get("fallback_engaged", 0),
        "fallback_recovered": events_by_name.get("fallback_recovered", 0),
        "fallback_failed": events_by_name.get("fallback_failed", 0),
        "fallback_reasons": dict(
            sorted(fallback_reasons.items(), key=lambda kv: -kv[1])
        ),
        "fallback_models": dict(fallback_models),
        "probe_failed_by_egress": dict(
            sorted(probe_by_egress.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "egress_recovered": events_by_name.get("egress_recovered", 0),
        "rotations_ok": rotation_ok,
        "rotations_failed": rotation_failed,
        "observed_ips": {k: sorted(v) for k, v in sorted(ips.items())},
    }
    return block, {"models": fallback_models}


def summarize(
    events: Sequence[Mapping[str, Any]],
    logs: Sequence[Mapping[str, Any]],
    *,
    window_hours: float = EVENTS_WINDOW_HOURS,
    log_tz_offset_s: float = 0.0,
) -> dict[str, Any]:
    """Fold raw records into the metric block the diagnosis reads.

    Per-bucket request counts always carry the bucket name and the observed
    egress IP (AGENTS.md observability rule: a count without both is not
    attributable and must not be reported).
    """

    windowed, window_info = window_events(
        events, logs, window_hours=window_hours, log_tz_offset_s=log_tz_offset_s
    )
    events_block, _ = _summarize_events(windowed)

    # The phase split needs two DIFFERENT log lines of the same turn: the
    # request record carries the total latency, the stream record carries the
    # relay duration. They are paired by request_id — the gateway logs them as
    # separate lines by design (routes/_pipeline.py).
    latency_by_request: dict[str, float] = {}
    for record in logs:
        rid = record.get("request_id")
        latency = _num(record.get("latency_ms"))
        if isinstance(rid, str) and rid and latency is not None:
            latency_by_request[rid] = latency

    requests: dict[str, int] = {}
    by_status: dict[str, int] = {}
    stream_reasons: dict[str, int] = {}
    total_latency: list[float] = []
    pre_byte: list[float] = []
    ttfb: list[float] = []
    per_bucket: dict[str, int] = {}
    fallback_served = 0
    fallback_served_by_model: dict[str, int] = {}
    egress_failovers = 0

    for record in logs:
        message = str(record.get("msg") or record.get("message") or "")
        if message.startswith("egress failover"):
            # An in-request bucket retry: the turn survived because a second
            # bucket answered. The phase split then contains a *whole* header
            # deadline plus the retry, which is the healthy shape — not
            # evidence that the deadline is too tight.
            egress_failovers += 1
        if message == "chat completion" or (
            "status" in record and "latency_ms" in record and "event" not in record
        ):
            if "status" in record:
                status = _num(record.get("status"))
                if status is not None:
                    key = str(int(status))
                    by_status[key] = by_status.get(key, 0) + 1
                    requests["total"] = requests.get("total", 0) + 1
                    if 200 <= int(status) < 300:
                        requests["ok"] = requests.get("ok", 0) + 1
                latency = _num(record.get("latency_ms"))
                if latency is not None:
                    total_latency.append(latency)
                fb = record.get("fallback")
                if isinstance(fb, str) and fb:
                    fallback_served += 1
                    model = str(record.get("fallback_model") or "")
                    if model:
                        fallback_served_by_model[model] = (
                            fallback_served_by_model.get(model, 0) + 1
                        )
                continue
        if message == "stream ended" or "reason" in record and "chunks" in record:
            reason = str(record.get("reason") or "unknown")
            stream_reasons[reason] = stream_reasons.get(reason, 0) + 1
            duration = _num(record.get("duration_ms"))
            latency = _num(record.get("latency_ms"))
            if latency is None:
                rid = record.get("request_id")
                if isinstance(rid, str):
                    latency = latency_by_request.get(rid)
            ttfb_ms = _num(record.get("ttfb_ms"))
            if ttfb_ms is not None and ttfb_ms >= 0:
                ttfb.append(ttfb_ms)
            if latency is not None and duration is not None and latency >= duration:
                pre_byte.append(latency - duration)
            egress = str(record.get("bucket_name") or record.get("egress") or "")
            if egress:
                per_bucket[egress] = per_bucket.get(egress, 0) + 1

    streams_total = sum(stream_reasons.values())
    clean = sum(stream_reasons.get(r, 0) for r in CLEAN_STREAM_REASONS)
    cut = stream_reasons.get("cut", 0)
    stalled = stream_reasons.get("stalled", 0)
    interrupted = stream_reasons.get("interrupted", 0)
    relay_errors = sum(v for k, v in stream_reasons.items() if k.startswith("relay_error"))
    client_side = (
        stream_reasons.get("client_disconnected", 0)
        + stream_reasons.get("client_stalled", 0)
    )
    accounted = clean + cut + stalled + interrupted + relay_errors + client_side
    undetermined = max(0, streams_total - accounted)

    total = int(requests.get("total", 0))
    ok = int(requests.get("ok", 0))
    return {
        "version": __version__,
        "events": events_block,
        "events_window": window_info,
        "requests": {
            "total": total,
            "ok": ok,
            "by_status": dict(sorted(by_status.items())),
            "ok_ratio": _ratio(ok, total),
            "latency_ms": {
                "p50": percentile(total_latency, 0.5),
                "p90": percentile(total_latency, 0.9),
                "p95": percentile(total_latency, 0.95),
                "max": percentile(total_latency, 1.0),
                "n": len(total_latency),
            },
            "pre_first_byte_ms": {
                "p50": percentile(pre_byte, 0.5),
                "p90": percentile(pre_byte, 0.9),
                "p95": percentile(pre_byte, 0.95),
                "max": percentile(pre_byte, 1.0),
                "n": len(pre_byte),
            },
        },
        "streams": {
            "total": streams_total,
            "by_reason": dict(sorted(stream_reasons.items())),
            "clean_ratio": _ratio(clean, streams_total),
            "cut_ratio": _ratio(cut, streams_total),
            "stalled_ratio": _ratio(stalled, streams_total),
            "undetermined_ratio": _ratio(undetermined, streams_total),
            "ttfb_ms": {
                "p50": percentile(ttfb, 0.5),
                "p95": percentile(ttfb, 0.95),
                "n": len(ttfb),
            },
        },
        "fallback": {
            "served_requests": fallback_served,
            "served_ratio": _ratio(fallback_served, total),
            "served_by_model": dict(
                sorted(fallback_served_by_model.items(), key=lambda kv: -kv[1])
            ),
        },
        "per_bucket": dict(sorted(per_bucket.items())),
        "egress": {"intra_request_failovers": egress_failovers},
    }


__all__ = [
    "CLEAN_STREAM_REASONS",
    "EVENTS_WINDOW_HOURS",
    "percentile",
    "summarize",
    "window_events",
]
