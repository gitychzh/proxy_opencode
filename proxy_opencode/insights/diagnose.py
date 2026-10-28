"""Diagnosis rules: metric block -> ordered, actionable findings.

Ordering is severity-first (critical -> warning -> info) and, inside a
severity, the cheapest-to-act on first. Every finding names its evidence so a
reader never has to trust the conclusion blindly.

Diagnosis order follows the 429 attribution red line in AGENTS.md (transport ->
prompt freshness -> session -> IP quota): a finding never tells the operator to
rotate an IP before the cheaper causes are excluded.
"""

from __future__ import annotations

from typing import Any, Mapping

# Share of finished streams that ended without a terminal chunk (a synthesised
# truncation) above which the upstream is cutting answers.
CUT_RATIO_WARN = 0.20
# Share of streams that went silent past the stall watchdog.
STALL_RATIO_WARN = 0.05
# Share of turns that never reached a terminal record at all.
UNDETERMINED_RATIO_WARN = 0.10
# Fallback trips (production incidents) per report window that make the primary
# chain "unstable" rather than "had one bad minute".
FALLBACK_INCIDENTS_WARN = 3
# Time-to-headers as a share of the configured header deadline above which the
# deadline is too tight (an attempt that close to the bound trips on a normal
# slow turn).
TTFB_DEADLINE_SHARE_WARN = 0.8
# Default header deadline when the caller does not pass the live setting.
DEFAULT_TTFB_DEADLINE_S = 60.0
# Probe healthy->unhealthy transitions per bucket per window above which the
# bucket is flapping rather than merely down (a sustained outage logs one).
# Anchored on the 2026-10-28 24h reading: 56 / 48 on the two legs of one
# device, 6 / 3 on the healthy boxes.
PROBE_FAILURES_WARN = 12
# Daily-quota cordons per window above which the per-IP capacity ceiling (and
# therefore the rotation threshold) needs a fresh measurement.
QUOTA_CORDONS_INFO = 10

# Cordon reasons that are a verdict about the egress IP itself (re-issuing the
# address clears them) — the only ones a rotation can cure.
IP_SCOPED_REASONS = frozenset({"free_quota_exhausted", "free_tier_rejected"})


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
    """Turn the metric block into ordered, actionable findings."""

    events = summary.get("events") or {}
    window: dict[str, Any] = dict(summary.get("events_window") or {})
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
    #    without proxy_ms the loop loses the property it depends on. The
    #    evidence carries the ledger window so a reader can tell "the backup
    #    is down NOW" from "the backup was down during the window".
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
                window_hours=window.get("hours"),
                window_anchor=window.get("anchor"),
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

    # 6. Header deadline too tight for the observed p95. The first client
    #    byte is bounded by the SAME deadline whether the answer streamed
    #    from upstream (measured by ``ttfb_ms``) or was buffered upstream and
    #    replayed as SSE (measured by the phase split — a synthesized stream
    #    reports a sub-millisecond ttfb because the replay has nothing to
    #    wait for, which used to make this rule unable to fire at all). Take
    #    the larger of the two phases: each stream record feeds only one of
    #    them, so the max is the honest "worst case before the client saw a
    #    byte".
    heads = [v for v in (ttfb_p95, pre_byte_p95) if isinstance(v, (int, float))]
    head_p95 = max(float(v) for v in heads) if heads else None
    failovers = int((summary.get("egress") or {}).get("intra_request_failovers") or 0)
    if (
        head_p95 is not None
        and ttfb_deadline_s > 0
        and head_p95 > ttfb_deadline_s * TTFB_DEADLINE_SHARE_WARN * 1000
    ):
        if failovers:
            # Measured 2026-10-28: 17:41:30 attempt on bucket #1, 17:42:31
            # TtfbTimeout exactly 60s later, retried on bucket #2, answered at
            # 17:42:34 — pre-first-byte 63.8s. The deadline did its job; the
            # bucket ate it. Raising ZEN_TTFB_S would park every turn on that
            # bucket 60+s longer, so the action must NOT be "raise the bound".
            findings.append(
                _finding(
                    "info",
                    "head_deadline_absorbed",
                    "首块耗时吃满 TTFB 期限后由换桶救回：期限在起作用，慢的是桶",
                    "查吃满期限的那个桶（agent 探针、隧道、ONT），"
                    "**不要**调大 ZEN_TTFB_S：调大只会让死桶多占更久。",
                    head_p95_ms=head_p95,
                    deadline_ms=ttfb_deadline_s * 1000,
                    intra_request_failovers=failovers,
                )
            )
        else:
            findings.append(
                _finding(
                    "warning",
                    "ttfb_deadline_tight",
                    "首块时延逼近 TTFB 期限，正常慢请求会被误判超时",
                    f"把 ZEN_TTFB_S 从 {ttfb_deadline_s:g}s 上调，或先查最慢桶；"
                    "调大前确认它不是黑洞跳的遮羞布：先看证据里 ttfb_p95 与"
                    "pre_first_byte_p95 哪个占主导——前者是上游响应头，后者是"
                    "桶租约等待加握手。",
                    ttfb_p95_ms=ttfb_p95,
                    pre_first_byte_p95_ms=pre_byte_p95,
                    head_p95_ms=head_p95,
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

    # 7. Rotation not landing: a repeated quota verdict that a fresh IP
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

    # 8. Quota burn without rotation configured.
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

    # 9. Ledger off / no traffic: the loop cannot see anything. But "0 kept"
    #    has two different causes with different actions: an actually-off
    #    ledger (dropped == 0, nothing was ever read) versus a live ledger
    #    whose every record the window cut away — which in production meant a
    #    clock mismatch, not a disabled sink (measured 2026-10-28: the desktop
    #    text log stamps LOCAL time while the ledger stamps UTC, so the
    #    anchor sat 8h in the future and all 52 records were dropped while
    #    the report still claimed "账本可能未开启", blinding the loop to
    #    fallback incidents).
    if not int(events.get("total") or 0):
        dropped = int(window.get("dropped") or 0)
        if dropped:
            finding = _finding(
                "info",
                "no_events_in_window",
                "账本有记录但全部落在窗口外：日志与账本的时钟/时区不一致",
                "先调大 --events-window-hours 复核；若锚点仍把账本全部裁掉，"
                "按生产者时钟差异处理——本机桌面日志为本地时间、账本为 UTC，"
                "自我优化 CLI 应传 --log-tz local（容器 TZ=UTC 时 local==utc），"
                "否则窗口对齐错位、兜底事件全部丢失。",
            )
            finding["evidence"].update(
                {
                    "dropped": dropped,
                    "kept": int(window.get("kept") or 0),
                    "undated": int(window.get("undated") or 0),
                    "anchor": window.get("anchor"),
                    "ledger_records_outside_window": dropped,
                }
            )
            findings.append(finding)
        else:
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

    # 10. Fallback serving an unexpected model = config drift.
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

    # 11. Probe flapping. Counted per healthy->unhealthy *transition*, so a
    #     high count means "this bucket keeps coming back and dropping again"
    #     while a bucket that died for 12h logs exactly one row. Measured
    #     2026-10-28 (24h window, 50 requests): owin10-telecom 56,
    #     owin10-mobile 48, cc9 6, esp32-bucket 3 — the two high ones are the
    #     two legs of ONE device (100.109.109.108:10001/10002), so the churn
    #     is device-side and the gateway's only lever is capacity planning.
    probe_by_egress = {
        str(k): int(v) for k, v in (events.get("probe_failed_by_egress") or {}).items()
    }
    worst_egress, worst_failures = max(
        probe_by_egress.items(), key=lambda kv: kv[1], default=("", 0)
    )
    hours = float(window.get("hours") or 0.0)
    if worst_failures >= PROBE_FAILURES_WARN:
        findings.append(
            _finding(
                "warning",
                "egress_probe_flapping",
                "出口探针反复抖动：桶在健康/不健康之间来回切换",
                "这是设备侧上行/隧道（agent not ready、ConnectTimeout），"
                "不是网关缺陷：查那台设备的网络与 agent 进程。"
                "**不要**为抖动调大探针容忍度来掩盖它——只在确认某桶长期"
                "不可用时才动 ZEN_EGRESS_PROXIES。",
                probe_failed_by_egress=probe_by_egress,
                worst_egress=worst_egress,
                worst_failures=worst_failures,
                per_hour=round(worst_failures / hours, 2) if hours > 0 else None,
                recovered_total=int(events.get("egress_recovered") or 0),
                window_hours=hours or None,
            )
        )

    # 12. Daily-quota cordons. This is the capacity ceiling the rotation
    #     threshold has to be judged against: report the observed burn rate
    #     instead of letting an operator guess whether *-500 is sane.
    quota_cordons = int(cordon_reasons.get("free_quota_exhausted") or 0)
    if quota_cordons >= QUOTA_CORDONS_INFO:
        findings.append(
            _finding(
                "info",
                "quota_cordons_high",
                "日配额 cordon 次数偏高：单 IP 可用请求数远低于轮换阈值",
                "按 429 归因红线，先把传输栈/body 新鲜度/session 三变量消融排除，"
                "再评估 ZEN_EGRESS_ROTATION 阈值是否高于单 IP 实际可服务请求数；"
                "在此之前不得仅凭 429 下调阈值。",
                quota_cordons=quota_cordons,
                rotations_ok=int(events.get("rotations_ok") or 0),
                window_hours=hours or None,
            )
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(str(f["severity"]), 3))
    return findings


__all__ = [
    "CUT_RATIO_WARN",
    "DEFAULT_TTFB_DEADLINE_S",
    "FALLBACK_INCIDENTS_WARN",
    "IP_SCOPED_REASONS",
    "PROBE_FAILURES_WARN",
    "QUOTA_CORDONS_INFO",
    "STALL_RATIO_WARN",
    "TTFB_DEADLINE_SHARE_WARN",
    "UNDETERMINED_RATIO_WARN",
    "diagnose",
]
