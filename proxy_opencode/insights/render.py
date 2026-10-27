"""Markdown rendering of one report (the artefact a human reads)."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


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


__all__ = ["render_markdown"]
