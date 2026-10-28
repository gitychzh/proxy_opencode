"""Tests for the log-driven self-optimization analysis (insights.py).

Every assertion is over a pure function with hand-written input, so a
regression in the metric math or a diagnosis threshold fails here instead of
producing a wrong optimization decision in production.
"""

from __future__ import annotations

import json

import pytest

from proxy_opencode.insights import (
    analyze,
    diagnose,
    parse_event_records,
    parse_log_lines,
    percentile,
    record_epoch,
    render_markdown,
    summarize,
    window_events,
)


def _event(**fields):
    return {"ts": "2026-10-27T00:00:00.000+00:00", **fields}


def _chat(status=200, latency_ms=1000.0, **extra):
    return {
        "msg": "chat completion",
        "status": status,
        "latency_ms": latency_ms,
        **extra,
    }


def _streamed(reason="completed", chunks=10, ttfb_ms=500.0, duration_ms=800.0, **extra):
    return {
        "msg": "stream ended",
        "reason": reason,
        "chunks": chunks,
        "ttfb_ms": ttfb_ms,
        "duration_ms": duration_ms,
        **extra,
    }


# ------------------------------------------------------------------ parsing


def test_parse_event_records_skips_torn_lines():
    text = "\n".join(
        [
            json.dumps(_event(event="cordon", egress="a", cordon_reason="x")),
            '{"event": "truncated',  # torn write after a rotation
            "",
            json.dumps(_event(event="rotation_succeeded", egress="a", ip_to="1.2.3.4")),
        ]
    )
    records = parse_event_records(text)
    assert [r["event"] for r in records] == ["cordon", "rotation_succeeded"]


def test_parse_log_lines_handles_text_and_json_formats():
    text_log = (
        "2026-10-27 22:00:00 INFO proxy_opencode.chat chat completion "
        "request_id=abc model=big-pickle status=200 latency_ms=1234.5 stream=True\n"
        "2026-10-27 22:00:01 WARNING proxy_opencode.pipeline client write stalled "
        "request_id=abc blocked_s=61.2\n"
    )
    records = parse_log_lines(text_log)
    assert records[0]["status"] == 200
    assert records[0]["latency_ms"] == 1234.5
    assert records[0]["msg"] == "chat completion"  # absent in text form
    assert records[1]["blocked_s"] == 61.2

    json_log = json.dumps(
        {"msg": "stream ended", "reason": "cut", "chunks": 3, "status": 200}
    )
    parsed = parse_log_lines(json_log + "\n")
    assert parsed == [{"msg": "stream ended", "reason": "cut", "chunks": 3, "status": 200}]


def test_parse_log_lines_ignores_noise():
    assert parse_log_lines("plain line with no pairs\n\n") == []


def test_record_epoch_accepts_every_shipped_timestamp_shape():
    """The ledger writes isoformat UTC, the text formatter writes a comma
    milliseconds run, and both must land on the same absolute clock."""
    reference = 1793124505.0  # 2026-10-27T18:08:25Z
    for ts in (
        "2026-10-27T18:08:25+00:00",
        "2026-10-27T18:08:25.000+00:00",
        "2026-10-27T18:08:25Z",
        "2026-10-27 18:08:25",
        "2026-10-27 18:08:25,000",
    ):
        assert record_epoch({"ts": ts}) == reference, ts
    # Nothing timestamped at all must be None, not a silent zero.
    assert record_epoch({}) is None
    assert record_epoch({"ts": "not-a-time"}) is None


# ---------------------------------------------------------------- windowing


def test_window_events_drops_ledger_history_older_than_the_log_window():
    """A 12-day ledger tail read against a 24h log dump must not be counted
    as one window: the mismatch invented a `fallback_backup_broken` critical
    off a failure 30 hours old while proxy_ms was healthy (2026-10-28)."""
    events = [
        {"ts": "2026-10-20T00:00:00+00:00", "event": "fallback_failed"},
        {"ts": "2026-10-27T12:00:00+00:00", "event": "fallback_engaged"},
    ]
    logs = [_chat(200, 1000.0, ts="2026-10-27T18:00:00+00:00")]
    kept, info = window_events(events, logs, window_hours=24.0)

    assert [r["event"] for r in kept] == ["fallback_engaged"]
    assert info["kept"] == 1 and info["dropped"] == 1
    assert info["anchor"] == "2026-10-27T18:00:00+00:00"


def test_window_events_anchors_on_the_newest_record_not_the_wall_clock():
    """Replaying a saved window must reproduce the numbers, so the anchor is
    the newest record — a wall-clock anchor would slide on every re-render."""
    events = [{"ts": "2026-01-01T00:00:00+00:00", "event": "cordon"}]
    logs = [_chat(200, 1000.0, ts="2026-01-02T00:00:00+00:00")]

    kept, info = window_events(events, logs, window_hours=24.0)
    assert kept == events  # exactly 24h back is inside the window
    assert info["dropped"] == 0


def test_window_events_keeps_undated_records_and_counts_them():
    events = [{"event": "cordon"}, {"epoch": 1}]
    logs = [_chat(200, 1000.0, ts="2026-10-27T18:00:00+00:00")]
    kept, info = window_events(events, logs, window_hours=24.0)

    assert len(kept) == 2, "an undated record cannot be attributed, not dropped"
    assert info["undated"] == 2


def test_window_events_zero_hours_disables_the_cut():
    events = [{"ts": "2000-01-01T00:00:00+00:00", "event": "cordon"}]
    logs = [_chat(200, 1000.0, ts="2026-10-27T18:00:00+00:00")]
    kept, info = window_events(events, logs, window_hours=0)
    assert len(kept) == 1 and info["dropped"] == 0


def test_summarize_reports_the_window_it_used():
    events = [
        {"ts": "2026-10-20T00:00:00+00:00", "event": "fallback_failed"},
        {"ts": "2026-10-27T12:00:00+00:00", "event": "fallback_engaged"},
    ]
    logs = [_chat(200, 1000.0, ts="2026-10-27T18:00:00+00:00")]
    s = summarize(events, logs)

    assert s["events"]["fallback_failed"] == 0, "the stale failure is out of window"
    assert s["events"]["fallback_incidents"] == 1
    assert s["events"]["total"] == 1
    assert s["events_window"]["dropped"] == 1
    assert s["events_window"]["hours"] == 24.0


def test_stale_ledger_failure_alone_no_longer_raises_a_critical():
    """The production shape of the false critical: a `fallback_failed` from
    30 hours ago, a healthy 24h log. Anchored at 18:00Z, anything before
    2026-10-26T18:00Z is out of window."""
    events_text = (
        json.dumps({"ts": "2026-10-25T18:42:49+00:00", "event": "fallback_failed"})
        + "\n"
    )
    log_text = (
        json.dumps(_chat(200, 1000.0, ts="2026-10-27T18:00:00+00:00", request_id="r1"))
        + "\n"
        + json.dumps(
            _streamed("completed", request_id="r1", ts="2026-10-27T18:00:01+00:00")
        )
        + "\n"
    )
    summary, findings = analyze(events_text, log_text)
    assert [f["key"] for f in findings if f["severity"] == "critical"] == []

    # The same failure inside the window is still a critical, with its window.
    fresh = (
        json.dumps({"ts": "2026-10-27T17:00:00+00:00", "event": "fallback_failed"})
        + "\n"
    )
    _, fresh_findings = analyze(fresh, log_text)
    critical = [f for f in fresh_findings if f["severity"] == "critical"]
    assert critical and critical[0]["evidence"]["window_hours"] == 24.0


# --------------------------------------------------------------- statistics


def test_percentile_nearest_rank_and_empty():
    assert percentile([], 0.95) is None
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.5) == 20.0
    assert percentile(values, 0.95) == 40.0
    assert percentile(values, 1.0) == 40.0
    assert percentile(values, 0.0) == 10.0


# --------------------------------------------------------------- summarising


def test_summarize_folds_events_logs_and_phase_split():
    events = [
        _event(event="cordon", egress="a",
               cordon_reason="free_quota_exhausted (per-IP daily quota)"),
        _event(event="cordon", egress="a", cordon_reason="free_quota_soft (short Retry-After)"),
        _event(event="fallback_engaged", reason="upstream HTTP 429",
               model="ZhipuAI/GLM-5.3-Flash"),
        _event(event="fallback_failed", reason="backup 429"),
        _event(event="rotation_succeeded", egress="a", ip_to="9.9.9.9"),
        _event(event="rotation_failed", egress="b"),
        _event(event="egress_ip_observed", egress="a", current="9.9.9.9"),
    ]
    logs = [
        _chat(200, 5000.0, request_id="r1", fallback="proxy_ms",
              fallback_model="ZhipuAI/GLM-5.3-Flash"),
        _chat(200, 1000.0, request_id="r2"),
        _chat(503, 900.0, request_id="r3"),
        _streamed("completed", request_id="r1", ttfb_ms=400.0, duration_ms=3000.0),
        _streamed("cut", request_id="r2", ttfb_ms=600.0, duration_ms=800.0),
    ]
    s = summarize(events, logs)

    assert s["events"]["total"] == 7
    assert s["events"]["cordon_reasons"] == {
        "free_quota_exhausted": 1,
        "free_quota_soft": 1,
    }
    assert s["events"]["fallback_incidents"] == 1
    assert s["events"]["fallback_failed"] == 1
    assert s["events"]["rotations_ok"] == 1
    assert s["events"]["rotations_failed"] == 1
    assert s["events"]["observed_ips"] == {"a": ["9.9.9.9"]}

    assert s["requests"]["total"] == 3
    assert s["requests"]["ok"] == 2
    assert s["requests"]["by_status"] == {"200": 2, "503": 1}
    # Phase split pairs the two lines of one turn by request_id:
    # 5000 total - 3000 stream duration = 2000ms before the first byte.
    assert s["requests"]["pre_first_byte_ms"]["n"] == 2
    assert s["requests"]["pre_first_byte_ms"]["max"] == 2000.0
    assert s["requests"]["pre_first_byte_ms"]["p50"] == 200.0
    assert s["requests"]["latency_ms"]["max"] == 5000.0

    assert s["streams"]["total"] == 2
    assert s["streams"]["cut_ratio"] == 0.5
    assert s["streams"]["clean_ratio"] == 0.5
    assert s["streams"]["ttfb_ms"]["p50"] == 400.0

    assert s["fallback"]["served_requests"] == 1
    assert s["fallback"]["served_by_model"] == {"ZhipuAI/GLM-5.3-Flash": 1}


def test_summarize_empty_inputs_are_zero_not_crash():
    s = summarize([], [])
    assert s["events"]["total"] == 0
    assert s["requests"]["total"] == 0
    assert s["requests"]["latency_ms"]["p50"] is None
    assert s["streams"]["cut_ratio"] == 0.0


# ---------------------------------------------------------------- diagnosis


def _summary(**overrides):
    base = summarize([], [])
    for key, value in overrides.items():
        base[key] = value
    return base


def _keys(findings):
    return [f["key"] for f in findings]


def test_diagnose_clean_window_has_no_findings():
    events = [_event(event="rotation_succeeded", egress="a", ip_to="1.1.1.1")]
    logs = [_chat(200, 1200.0), _streamed("completed")]
    summary = summarize(events, logs)
    assert diagnose(summary) == []


def test_diagnose_backup_failure_is_critical_and_first():
    events = [
        _event(event="fallback_engaged", reason="upstream HTTP 429"),
        _event(event="fallback_failed", reason="backup 429"),
    ]
    logs = [_chat(200, 1000.0), _streamed("cut")]
    findings = diagnose(summarize(events, logs))
    assert findings[0]["key"] == "fallback_backup_broken"
    assert findings[0]["severity"] == "critical"


def test_diagnose_flags_unstable_primary_with_attribution_order():
    events = [
        _event(event="fallback_engaged", reason="upstream HTTP 429")
        for _ in range(4)
    ]
    findings = diagnose(summarize(events, [_chat(200, 1000.0)]))
    item = next(f for f in findings if f["key"] == "primary_unstable")
    assert item["severity"] == "warning"
    assert "传输栈" in item["action"] and "IP 配额" in item["action"]


def test_diagnose_flags_probe_flapping_per_bucket():
    """One device's two legs flipping all day is a device problem, not a
    gateway one — and it must be visible in the report, not only in the raw
    log. Measured 2026-10-28: 56 / 48 transitions on the two ports of
    100.109.109.108 while the other buckets sat at 6 / 3."""
    events = [
        _event(event="probe_failed", egress="owin10-telecom", reason="agent not ready")
        for _ in range(56)
    ] + [
        _event(event="probe_failed", egress="owin10-mobile", reason="agent not ready")
        for _ in range(48)
    ] + [
        _event(event="probe_failed", egress="cc9", reason="ConnectTimeout")
        for _ in range(6)
    ]
    findings = diagnose(summarize(events, [_chat(200, 1000.0)]))
    item = next(f for f in findings if f["key"] == "egress_probe_flapping")
    assert item["severity"] == "warning"
    assert item["evidence"]["worst_egress"] == "owin10-telecom"
    assert item["evidence"]["worst_failures"] == 56
    assert item["evidence"]["probe_failed_by_egress"]["cc9"] == 6
    # The action must not invite an IP rotation: this is not a 429 at all.
    assert "设备侧" in item["action"] and "ZEN_EGRESS_PROXIES" in item["action"]


def test_diagnose_stays_quiet_on_a_single_probe_transition():
    """A genuinely dead bucket logs ONE transition (pool.py logs per state
    change), so it must not be reported as flapping."""
    events = [_event(event="probe_failed", egress="cc9", reason="ConnectTimeout")]
    findings = diagnose(summarize(events, [_chat(200, 1000.0)]))
    assert "egress_probe_flapping" not in _keys(findings)


def test_diagnose_flags_quota_cordon_rate_without_touching_rotation():
    events = [
        _event(event="cordon", egress="cc9", cordon_reason="free_quota_exhausted")
        for _ in range(12)
    ] + [_event(event="rotation_succeeded", egress="cc9", ip_to="1.2.3.4")]
    findings = diagnose(summarize(events, [_chat(200, 1000.0)]))
    item = next(f for f in findings if f["key"] == "quota_cordons_high")
    assert item["severity"] == "info"
    assert item["evidence"]["quota_cordons"] == 12
    # Red line: the action may never say "lower the rotation threshold now".
    assert "不得仅凭 429 下调阈值" in item["action"]


def test_diagnose_flags_high_cut_and_stall_ratios():
    logs = [_streamed("cut") for _ in range(8)] + [_streamed("completed") for _ in range(2)]
    findings = diagnose(summarize([], logs))
    assert "stream_cut_high" in _keys(findings)

    logs = [_streamed("stalled") for _ in range(2)] + [
        _streamed("completed") for _ in range(8)
    ]
    findings = diagnose(summarize([], logs))
    assert "stream_stalled_high" in _keys(findings)


def test_diagnose_flags_undetermined_streams():
    """A stream record with an unknown reason is neither clean nor a known
    failure: the relay never classified it (process killed / log truncated)."""
    logs = [_streamed("weird_reason") for _ in range(3)] + [_streamed("completed")]
    findings = diagnose(summarize([], logs))
    assert "stream_undetermined_high" in _keys(findings)


def test_diagnose_flags_tight_ttfb_deadline():
    logs = [_streamed("completed", ttfb_ms=50_000.0)]
    findings = diagnose(summarize([], logs), ttfb_deadline_s=60.0)
    assert "ttfb_deadline_tight" in _keys(findings)


def test_diagnose_deadline_rule_sees_the_pre_first_byte_phase():
    """A synthesized stream (buffered upstream, replayed as SSE) reports a
    sub-millisecond ttfb_ms, so the header-deadline rule could never fire on
    this workload. The phase split carries the real wait instead: measured
    2026-10-28 production had ttfb_ms=0.3 with 63.8s before the first byte,
    i.e. the deadline risk was invisible."""
    logs = [
        _chat(200, 63_897.0, request_id="r1"),
        _streamed("completed", request_id="r1", ttfb_ms=0.3, duration_ms=57.0),
    ]
    summary = summarize([], logs)
    assert summary["streams"]["ttfb_ms"]["p95"] == 0.3
    assert summary["requests"]["pre_first_byte_ms"]["p95"] == 63_840.0

    findings = diagnose(summary, ttfb_deadline_s=60.0)
    item = next(f for f in findings if f["key"] == "ttfb_deadline_tight")
    assert item["evidence"]["head_p95_ms"] == 63_840.0
    assert item["evidence"]["ttfb_p95_ms"] == 0.3


def test_diagnose_deadline_rule_stays_quiet_on_fast_turns():
    logs = [
        _chat(200, 3000.0, request_id="r1"),
        _streamed("completed", request_id="r1", ttfb_ms=900.0, duration_ms=2000.0),
    ]
    findings = diagnose(summarize([], logs), ttfb_deadline_s=60.0)
    assert "ttfb_deadline_tight" not in _keys(findings)


def test_diagnose_separates_a_bucket_eating_the_deadline_from_a_tight_deadline():
    """63.8s pre-first-byte is healthy when an in-request bucket retry paid
    the 60s deadline and the next bucket answered in 3.9s: the bound worked,
    the bucket did not. The action must then be "look at that bucket", never
    "raise ZEN_TTFB_S" (which would park every turn on the dead bucket)."""
    logs = [
        {"msg": "egress failover: upstream transport on one bucket, "
                "retrying on another (2 attempt(s) left)"},
        _chat(200, 63_897.0, request_id="r1"),
        _streamed("completed", request_id="r1", ttfb_ms=0.3, duration_ms=57.0),
    ]
    summary = summarize([], logs)
    assert summary["egress"]["intra_request_failovers"] == 1

    findings = diagnose(summary, ttfb_deadline_s=60.0)
    keys = _keys(findings)
    assert "head_deadline_absorbed" in keys
    assert "ttfb_deadline_tight" not in keys
    item = next(f for f in findings if f["key"] == "head_deadline_absorbed")
    assert item["severity"] == "info"
    assert "不要" in item["action"] and "ZEN_TTFB_S" in item["action"]


def test_diagnose_flags_rotation_not_landing():
    events = [
        _event(event="cordon", egress="a",
               cordon_reason="free_quota_exhausted (per-IP daily quota)"),
        _event(event="rotation_failed", egress="a"),
    ]
    findings = diagnose(summarize(events, [_chat(200, 1000.0)]))
    item = next(f for f in findings if f["key"] == "rotation_not_landing")
    assert "换 IP 只对" in item["action"]


def test_diagnose_flags_missing_ledger_and_missing_traffic():
    findings = diagnose(summarize([], []))
    keys = _keys(findings)
    assert "no_events" in keys and "no_traffic" in keys


def test_diagnose_flags_fallback_model_drift():
    logs = [_chat(200, 1000.0, fallback="proxy_ms", fallback_model="some/other-model")]
    findings = diagnose(
        summarize([], logs), fallback_model_hint="ZhipuAI/GLM-5.3-Flash"
    )
    assert "fallback_model_drift" in _keys(findings)


def test_diagnose_orders_by_severity():
    events = [
        _event(event="fallback_engaged", reason="upstream HTTP 429"),
        _event(event="fallback_failed", reason="backup 429"),
    ] + [_event(event="fallback_engaged", reason="upstream HTTP 429") for _ in range(3)]
    findings = diagnose(summarize(events, [_chat(200, 1000.0)]))
    severities = [f["severity"] for f in findings]
    assert severities == sorted(
        severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s]
    )


# ------------------------------------------------------------------ render


def test_render_markdown_contains_headline_and_actions():
    events = [_event(event="fallback_failed", reason="backup 429")]
    summary, findings = analyze(
        "\n".join(json.dumps(e) for e in events),
        json.dumps(_chat(200, 1000.0)) + "\n",
    )
    text = render_markdown(summary, findings)
    assert "proxy_opencode 自我优化报告" in text
    assert "兜底失败 1" in text
    assert "[严重]" in text and "动作：" in text


def test_render_markdown_clean_case_states_healthy():
    summary, findings = analyze("", "", )
    assert findings, "an empty window must still report that it saw nothing"
    text = render_markdown(summary, [])
    assert "主链、出口池与兜底链均按预期工作" in text

    # A window with traffic AND a live ledger and no rule tripped: healthy.
    healthy, healthy_findings = analyze(
        json.dumps(_event(event="rotation_succeeded", egress="a", ip_to="1.1.1.1"))
        + "\n",
        json.dumps(_chat(200, 1000.0, request_id="r1")) + "\n"
        + json.dumps(_streamed("completed", request_id="r1")) + "\n",
    )
    assert healthy_findings == []
    assert "主链、出口池与兜底链均按预期工作" in render_markdown(healthy, healthy_findings)


# --------------------------------------------------------------------- CLI


def test_cli_exit_codes(tmp_path):
    import scripts.self_optimize as cli

    ok_events = tmp_path / "events.jsonl"
    ok_events.write_text(
        json.dumps(_event(event="rotation_succeeded", egress="a", ip_to="1.1.1.1")) + "\n",
        encoding="utf-8",
    )
    ok_log = tmp_path / "gw.log"
    ok_log.write_text(
        json.dumps(_chat(200, 1000.0)) + "\n" + json.dumps(_streamed("completed")) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.md"
    code = cli.main(
        ["--events", str(ok_events), "--log", str(ok_log), "--out", str(out)]
    )
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("# proxy_opencode 自我优化报告")

    bad_events = tmp_path / "bad.jsonl"
    bad_events.write_text(
        json.dumps(_event(event="fallback_failed", reason="backup 429")) + "\n",
        encoding="utf-8",
    )
    assert cli.main(["--events", str(bad_events), "--log", str(ok_log), "--json"]) == 1


def test_cli_requires_an_input(argv_probe=None):
    import scripts.self_optimize as cli

    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
