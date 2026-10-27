"""Log-driven self-optimization: turn the gateway's own telemetry into findings.

Inputs are the three observation surfaces the gateway already produces, and
all three are red-line clean (no prompts, no message content, no keys):

* the JSONL egress event ledger (``ZEN_EGRESS_EVENT_LOG``) — cordons,
  rotations, fallback engaged/failed/recovered, window rollovers, IP changes;
* request / stream log lines (text or ``LOG_FORMAT=json``) — the
  ``chat completion`` record (status, latency_ms, usage, fallback provenance)
  and the ``stream ended`` record (chunks, ttfb_ms, duration_ms, reason);
* optionally a ``/admin/egresses`` snapshot.

Everything below is a pure function over parsed records, so the loop's
conclusions are deterministic and unit-testable instead of relying on a
human reading dashboards: same input -> same findings, every run.

Diagnosis order follows the 429 attribution red line in AGENTS.md
(transport -> prompt freshness -> session -> IP quota): a finding never
tells the operator to rotate an IP before the cheaper causes are excluded.

The CLI wrapper is ``scripts/self_optimize.py``; the loop that consumes its
output is documented in ``docs/SELF_OPTIMIZATION.md``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from . import __version__

# --------------------------------------------------------------- thresholds

# Share of finished streams that ended without a terminal chunk (a
# synthesised truncation) above which the upstream is cutting answers.
CUT_RATIO_WARN = 0.20
# Share of streams that went silent past the stall watchdog.
STALL_RATIO_WARN = 0.05
# Share of turns that never reached a terminal record at all.
UNDETERMINED_RATIO_WARN = 0.10
# Fallback trips (production incidents) per report window that make the
# primary chain "unstable" rather than "had one bad minute".
FALLBACK_INCIDENTS_WARN = 3
# Time-to-headers as a share of the configured header deadline above which
# the deadline is too tight (an attempt that close to the bound will trip on
# a normal slow turn).
TTFB_DEADLINE_SHARE_WARN = 0.8
# Default header deadline when the caller does not pass the live setting.
DEFAULT_TTFB_DEADLINE_S = 60.0

_FALLBACK_EVENTS = frozenset(
    {"fallback_engaged", "fallback_failed", "fallback_recovered"}
)
# Cordon reasons that are a verdict about the egress IP itself (re-issuing
# the address clears them) — the only ones a rotation can cure.
IP_SCOPED_REASONS = frozenset({"free_quota_exhausted", "free_tier_rejected"})
# Stream end outcomes that mean "the client got a clean, complete answer".
CLEAN_STREAM_REASONS = frozenset({"completed"})

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
# Text formatter head: "<date> <time> <LEVEL> <logger> <message><extras>".
_TEXT_HEAD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T][\d:.,]+ +[A-Z]+ +(\S+) +(.*)$"
)
# Where the trailing key=value run starts; everything before it is the message.
_KV_START_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_]*=")


# ------------------------------------------------------------------ parsing


def parse_event_records(text: str) -> list[dict[str, Any]]:
    """Parse JSONL ledger text into records (malformed lines are skipped).

    A corrupt line must never abort the report: the ledger is appended to by
    a rotating writer, so a torn final line after a rotation is expected.
    """

    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _coerce(token: str) -> Any:
    if token in ("null", "None", ""):
        return None
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def parse_log_lines(text: str) -> list[dict[str, Any]]:
    """Parse gateway log lines into dicts of key=value extras.

    Accepts both shipped formats:

    * ``LOG_FORMAT=json`` — one JSON object per line (extras already typed);
    * the text formatter — ``<ts> <LEVEL> <logger> <msg> k=v k=v ...``.

    Values containing spaces (``cordon_reason=free_quota_exhausted (per-IP
    daily quota)``) are truncated at the first space by construction; every
    consumer here keys off the prefix, so that is harmless and keeps the
    parser a single regex instead of a quoting grammar.
    """

    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict):
                out.append(record)
                continue
        extras: dict[str, Any] = {}
        head = _TEXT_HEAD_RE.match(line)
        if head is not None:
            # The message text is what classifies a line as a request record
            # or a stream record, so it must survive parsing: without it the
            # default (text) log format yields unattributable key/value soup.
            extras["logger"] = head.group(1)
            extras["msg"] = _KV_START_RE.split(head.group(2), maxsplit=1)[0].strip()
        for key, value in _KV_RE.findall(line):
            extras[key] = _coerce(value)
        if extras:
            out.append(extras)
    return out


# --------------------------------------------------------------- statistics


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


# --------------------------------------------------------------- summarising


def _event_name(record: Mapping[str, Any]) -> str:
    name = record.get("event")
    return str(name) if isinstance(name, str) else ""


def summarize(
    events: Sequence[Mapping[str, Any]],
    logs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fold raw records into the metric block the diagnosis reads.

    Per-bucket request counts always carry the bucket name and the observed
    egress IP (AGENTS.md observability rule: a count without both is not
    attributable and must not be reported).
    """

    events_by_name: dict[str, int] = {}
    cordon_reasons: dict[str, int] = {}
    fallback_reasons: dict[str, int] = {}
    fallback_models: dict[str, int] = {}
    rotation_ok = 0
    rotation_failed = 0
    ips: dict[str, set[str]] = {}

    for record in events:
        name = _event_name(record)
        if not name:
            continue
        events_by_name[name] = events_by_name.get(name, 0) + 1
        if name == "cordon":
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

    requests: dict[str, int] = {}
    by_status: dict[str, int] = {}
    stream_reasons: dict[str, int] = {}
    total_latency: list[float] = []
    pre_byte: list[float] = []
    ttfb: list[float] = []
    per_bucket: dict[str, int] = {}
    fallback_served = 0
    fallback_served_by_model: dict[str, int] = {}

    # The phase split needs two DIFFERENT log lines of the same turn: the
    # request record carries the total latency, the stream record carries the
    # relay duration. They are paired by request_id — the gateway logs them
    # as separate lines by design (routes/_pipeline.py), so a per-line
    # subtraction would always see one of the two fields missing.
    latency_by_request: dict[str, float] = {}
    for record in logs:
        rid = record.get("request_id")
        latency = _num(record.get("latency_ms"))
        if isinstance(rid, str) and rid and latency is not None:
            latency_by_request[rid] = latency

    for record in logs:
        message = str(record.get("msg") or record.get("message") or "")
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
            # Phase split (2026-10-26 排障口诀): total minus stream duration
            # is the time spent before the first body byte — the only phase
            # the stream watchdogs cannot see.
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
        "events": {
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
            "rotations_ok": rotation_ok,
            "rotations_failed": rotation_failed,
            "observed_ips": {k: sorted(v) for k, v in sorted(ips.items())},
        },
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
    }


# ---------------------------------------------------------------- diagnosis


def _finding(
    severity: str, key: str, title: str, action: str, **evidence: Any
) -> dict[str, Any]:
    return {
        "severity": severity,
        "key": key,
        "title": title,
        "evidence": evidence,
        "action": action,
    }


def diagnose(
    summary: Mapping[str, Any],
    *,
    ttfb_deadline_s: float = DEFAULT_TTFB_DEADLINE_S,
    fallback_model_hint: str = "",
) -> list[dict[str, Any]]:
    """Turn the metric block into ordered, actionable findings.

    Ordering is severity-first (critical -> warning -> info) and, inside a
    severity, the cheapest-to-act on first. Every finding names its evidence
    so a reader never has to trust the conclusion blindly.
    """

    events = summary.get("events") or {}
    requests = summary.get("requests") or {}
    streams = summary.get("streams") or {}
    fallback = summary.get("fallback") or {}
    findings: list[dict[str, Any]] = []

    failed = int(events.get("fallback_failed") or 0)
    incidents = int(events.get("fallback_incidents") or 0)
    recovered = int(events.get("fallback_recovered") or 0)
    cordon_reasons: dict[str, int] = dict(events.get("cordon_reasons") or {})
    rotations_failed = int(events.get("rotations_failed") or 0)
    cut_ratio = float(streams.get("cut_ratio") or 0.0)
    stalled_ratio = float(streams.get("stalled_ratio") or 0.0)
    undetermined = float(streams.get("undetermined_ratio") or 0.0)
    ttfb_p95 = (streams.get("ttfb_ms") or {}).get("p95")
    pre_byte_p95 = (requests.get("pre_first_byte_ms") or {}).get("p95")

    # 1. The safety net itself is broken — this outranks everything else:
    #    without proxy_ms the loop loses the property it depends on.
    if failed:
        findings.append(
            _finding(
                "critical",
                "fallback_backup_broken",
                "兜底链自身失败：主链与 proxy_ms 同时不可用",
                "先恢复 proxy_ms（余额/可达性/密钥），再谈其它优化；"
                "期间任何主链故障都会直达客户端。",
                fallback_failed=failed,
                incidents=incidents,
            )
        )

    # 2. Primary chain flapping: engagement + no matching recovery means the
    #    client keeps switching models under its feet.
    if incidents >= FALLBACK_INCIDENTS_WARN:
        reasons: dict[str, int] = dict(events.get("fallback_reasons") or {})
        findings.append(
            _finding(
                "warning",
                "primary_unstable",
                "主链不稳定：报告窗口内多次触发兜底",
                "按 429 归因红线顺序排查：传输栈(bun) → prompt 新鲜度 → "
                "session 单次使用 → 才怀疑 IP 配额；不要先换 IP。",
                incidents=incidents,
                recovered=recovered,
                reasons=dict(list(reasons.items())[:5]),
            )
        )

    # 3. Stream truncation: upstream cuts long answers (synthesised
    #    finish_reason=length terminators hide it from the wire).
    if cut_ratio > CUT_RATIO_WARN:
        findings.append(
            _finding(
                "warning",
                "stream_cut_high",
                "上游截断率偏高：大量流以合成终止符收尾",
                "核对 body 门（prompt 日期/模型名/工作区是否本请求现场渲染）"
                "与 max_tokens；必要时降低单轮输出长度。",
                cut_ratio=cut_ratio,
                by_reason=dict(streams.get("by_reason") or {}),
            )
        )

    # 4. Stall watchdog trips: egress path jitter, not an upstream verdict.
    if stalled_ratio > STALL_RATIO_WARN:
        findings.append(
            _finding(
                "warning",
                "stream_stalled_high",
                "流出站静默超时比例偏高：出口链路抖动",
                "查这三桶的 probe 失败/连接超时事件；"
                "不要在未排除传输问题前把 429 当配额处理。",
                stalled_ratio=stalled_ratio,
                by_reason=dict(streams.get("by_reason") or {}),
            )
        )

    # 5. Streams with no terminal record at all: the relay never finished
    #    (process killed, worker cancelled) so no end_reason was ever logged.
    if undetermined > UNDETERMINED_RATIO_WARN:
        findings.append(
            _finding(
                "warning",
                "stream_undetermined_high",
                "部分流没有收尾记录：进程被中断或日志被截断",
                "查容器重启/OOM 与日志轮转（eventlog 按 5MB 轮转，docker logs "
                "需 --since 对齐窗口）；这类尾迹不代表上游故障。",
                undetermined_ratio=undetermined,
                total_streams=int(streams.get("total") or 0),
            )
        )

    # 6. Header deadline too tight for the observed p95.
    if (
        isinstance(ttfb_p95, (int, float))
        and ttfb_deadline_s > 0
        and float(ttfb_p95) > ttfb_deadline_s * TTFB_DEADLINE_SHARE_WARN * 1000
    ):
        findings.append(
            _finding(
                "warning",
                "ttfb_deadline_tight",
                "首块时延逼近 TTFB 期限，正常慢请求会被误判超时",
                f"把 ZEN_TTFB_S 从 {ttfb_deadline_s:g}s 上调，或先查最慢桶；"
                "调大前确认它不是黑holed 跳的遮羞布。",
                ttfb_p95_ms=ttfb_p95,
                deadline_ms=ttfb_deadline_s * 1000,
            )
        )
    if isinstance(pre_byte_p95, (int, float)) and float(pre_byte_p95) > 20000:
        findings.append(
            _finding(
                "info",
                "pre_byte_slow",
                "首字节前耗时 p95 偏高（watchdog 覆盖不到的阶段）",
                "对比 ZEN_FALLBACK_PRIMARY_TIMEOUT_S；该阶段慢就是桶或上游"
                "握手慢，靠 stream 看门狗看不出来。",
                pre_first_byte_p95_ms=pre_byte_p95,
            )
        )

    # 6. Rotation not landing: a repeated quota verdict that a fresh IP
    #    should have cleared means the ONT flow is broken, not the account.
    ip_scoped = {k: v for k, v in cordon_reasons.items() if k in IP_SCOPED_REASONS}
    if ip_scoped and rotations_failed:
        findings.append(
            _finding(
                "warning",
                "rotation_not_landing",
                "换 IP 未落地：IP 相关 cordon 之后仍有换 IP 失败",
                "核对 ONT 配置/prefix 与 agent /rotate 可达性；"
                "换 IP 只对「真实 IP 配额」有效，对①传输②body③session 无效。",
                rotations_failed=rotations_failed,
                ip_scoped_cordons=ip_scoped,
            )
        )

    # 7. Quota burn without rotation configured.
    if cordon_reasons.get("free_quota_exhausted") and not events.get("rotations_ok"):
        findings.append(
            _finding(
                "info",
                "quota_burn_no_rotation",
                "出现日配额 cordon，但本窗口没有成功换 IP",
                "确认 ZEN_EGRESS_ROTATION 是否配置（阈值轮换可在烧穿前主动换 IP）。",
                cordons=cordon_reasons.get("free_quota_exhausted"),
            )
        )

    # 8. Ledger off / no traffic: the loop cannot see anything.
    if not int(events.get("total") or 0):
        findings.append(
            _finding(
                "info",
                "no_events",
                "没有读到任何出口事件：事件账本可能未开启",
                "设置 ZEN_EGRESS_EVENT_LOG=/data/egress_events.jsonl 并确认 /data "
                "对 uid 10002 可写；否则 cordon/换 IP/兜底全部无法归因。",
            )
        )
    total_requests = int(requests.get("total") or 0)
    if not total_requests:
        findings.append(
            _finding(
                "info",
                "no_traffic",
                "报告窗口内没有请求记录：自我优化闭环缺少输入",
                "让客户端（Hermes/WorkBuddy）先跑一轮真实流量再分析；"
                "日志管线本身也就此得到验证。",
            )
        )

    # 9. Fallback serving an unexpected model = config drift.
    served_models = dict(fallback.get("served_by_model") or {})
    if fallback_model_hint and served_models:
        unexpected = [m for m in served_models if m != fallback_model_hint]
        if unexpected:
            findings.append(
                _finding(
                    "info",
                    "fallback_model_drift",
                    "兜底链实际服务的模型与预期不一致",
                    f"核对 ZEN_FALLBACK_DEFAULT_MODEL（预期 {fallback_model_hint}）"
                    "与 ZEN_FALLBACK_MODEL_MAP。",
                    expected=fallback_model_hint,
                    served=served_models,
                )
            )

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(str(f["severity"]), 3))
    return findings


# ------------------------------------------------------------------ render


def render_markdown(
    summary: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    *,
    title: str = "proxy_opencode 自我优化报告",
) -> str:
    """Human-readable report (the deliverable the loop hands to a human)."""

    events = summary.get("events") or {}
    requests = summary.get("requests") or {}
    streams = summary.get("streams") or {}
    fallback = summary.get("fallback") or {}
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- 网关版本：`{summary.get('version')}`")
    lines.append(
        f"- 事件账本：{int(events.get('total') or 0)} 条"
        f"（兜底触发 {int(events.get('fallback_incidents') or 0)} / "
        f"恢复 {int(events.get('fallback_recovered') or 0)} / "
        f"兜底失败 {int(events.get('fallback_failed') or 0)}）"
    )
    lines.append(
        f"- 请求：{int(requests.get('total') or 0)} 条，成功 {int(requests.get('ok') or 0)}"
        f"（{float(requests.get('ok_ratio') or 0) * 100:.1f}%），"
        f"状态分布 {dict(requests.get('by_status') or {})}"
    )
    latency = requests.get("latency_ms") or {}
    pre = requests.get("pre_first_byte_ms") or {}
    lines.append(
        f"- 时延：总 p50/p95/max = {latency.get('p50')}/{latency.get('p95')}/"
        f"{latency.get('max')} ms；首字节前 p50/p95 = {pre.get('p50')}/{pre.get('p95')} ms"
    )
    lines.append(
        f"- 流：{int(streams.get('total') or 0)} 条，干净收尾 "
        f"{float(streams.get('clean_ratio') or 0) * 100:.1f}%，截断 "
        f"{float(streams.get('cut_ratio') or 0) * 100:.1f}%，静默超时 "
        f"{float(streams.get('stalled_ratio') or 0) * 100:.1f}%"
    )
    lines.append(
        f"- 兜底服务：{int(fallback.get('served_requests') or 0)} 条请求"
        f"（{float(fallback.get('served_ratio') or 0) * 100:.1f}%），"
        f"模型 {dict(fallback.get('served_by_model') or {})}"
    )
    buckets = summary.get("per_bucket") or {}
    if buckets:
        lines.append(f"- 按桶流计数：{dict(buckets)}")
    ips = events.get("observed_ips") or {}
    if ips:
        lines.append(f"- 观测到的出口 IP：{dict(ips)}")
    lines.append("")
    if not findings:
        lines.append("## 结论")
        lines.append("")
        lines.append("窗口内未触发任何诊断规则：主链、出口池与兜底链均按预期工作。")
        return "\n".join(lines) + "\n"

    lines.append("## 发现与动作")
    lines.append("")
    for item in findings:
        sev = str(item.get("severity") or "info")
        badge = {"critical": "严重", "warning": "警告", "info": "提示"}.get(sev, sev)
        lines.append(f"### [{badge}] {item.get('title')}")
        lines.append("")
        lines.append(f"- 证据：`{json.dumps(item.get('evidence') or {}, ensure_ascii=False)}`")
        lines.append(f"- 动作：{item.get('action')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def analyze(
    event_text: str,
    log_text: str,
    *,
    ttfb_deadline_s: float = DEFAULT_TTFB_DEADLINE_S,
    fallback_model_hint: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One-shot pipeline: raw text in, (summary, findings) out."""

    summary = summarize(
        parse_event_records(event_text), parse_log_lines(log_text)
    )
    findings = diagnose(
        summary,
        ttfb_deadline_s=ttfb_deadline_s,
        fallback_model_hint=fallback_model_hint,
    )
    return summary, findings


__all__ = [
    "analyze",
    "diagnose",
    "parse_event_records",
    "parse_log_lines",
    "percentile",
    "render_markdown",
    "summarize",
]
