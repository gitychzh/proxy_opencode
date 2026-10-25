# BUG_REPORT_2026-10-26 — 生产日志系统性排障（0.7.0a2 → 0.7.0a3）

> 对象：杭州 ECS `zen-gw` 容器（公网入口 `https://115.29.231.25:10002/v1`）。
> 数据窗口：2026-10-25 00:00Z – 2026-10-26 01:00 CST（容器日志 + `/data/egress_events.jsonl`
> + `/admin/egresses`）。提交：`1c2413d`，镜像 `zen-gw:0.7.0a3`，已上线。

## 0. 结论摘要

| # | 缺陷 | 根因 | 影响 | 状态 |
|---|---|---|---|---|
| 1 | 305–315s 慢请求 | 上游跳只有「整个交换 300s」一个上界，无首字节超时 | 单请求白等 5 分钟才换桶 | 已修 |
| 2 | 僵尸桶 | 探针传输异常逃逸，未落 `healthy=false` | 死 agent 继续接客，连续 transport 失败 | 已修 |
| 3 | cordon 洪水 | 每个 429 都重设到 UTC0 且重发 ERROR/事件 | 6h 48 条 ERROR、单日 253 事件淹掉账本 | 已修 |
| 4 | 软 429 被当日报废 | `Retry-After ≤ 3600s` 也禁到窗口末 | 一次 30s 节流罚掉一整天容量 | 已修 |
| 5 | telecom 死桶 | `expect_prefix` 被当 cordon 解除的硬前置 | 单日 11/21 次轮换判失败、桶全天不可用 | 已修 |
| 6 | 熔断抖动 | 单次成功即 RECOVERED、单次失败即 ENGAGED | 单日 57 engaged / 11 recovered，模型来回切换 | 已修 |
| 7 | 400 误归因 | 管线把任何 `ValueError` 映射成 `invalid_request_error` | 上游解析意外被说成客户端错，且绕过桶间 failover | 已修 |

## 1. 证据

### 1.1 慢请求分相
`chat completion.latency_ms` 减去同 `request_id` 的 `stream ended.duration_ms` 即「首字节前」耗时：

```
n=354（12h，仅 200）
p50 5.9s | p90 12.2s | p95 21.3s | p99 304.6s
> 45s: 7 个，其中 6 个落在 304–312s，其后的流只需 0.5–4s
```

6 个 300s 级请求 = `zen_timeout_s` 被打满后重试成功，即「隧道黑洞 + 无首字节超时」。

### 1.2 出口池与事件账本（单日）
```
cordon 253   cordon_lifted 9   request_on_cordoned_bucket 244
rotation_requested 72（mobile 35 / telecom 21 / cc9 16）
fallback_engaged 57   fallback_recovered 11   fallback_failed 6
轮换间隔：hard_429 触发多次落在 0.3–4 分钟（下限 1800s 未覆盖 403 分支）
```

### 1.3 前缀漂移
`owin10-telecom` 现行出口 `117.95.178.237`，其 `expect_prefix` 只有
`106.111/106.110/121.226/121.234/180.117.204/114.239` → `egress_ip_matches_prefix=false`，
而 `_lift_ip_scoped_cordon` 与 `_wait_for_ip_change` 都以该前缀为硬条件 ⇒ 该桶的
配额 cordon 无法解除、轮换永远记失败（当日 11/21）。

### 1.4 400
24h 内仅 1 次：`stream=false, tools=false`，2.2s，`usage=null`。管线 `ValueError → 400`
是唯一能产生这种组合的路径（上游 400 会原样中继，但不会在 2.2s 后才决定）。

## 2. 修复要点

1. **首字节超时**：`forward.ts` 增 `ZEN_BUN_TTFB_MS`（响应头到达即清除定时器），
   Python 侧 META 等待同预算兜底，配置 `ZEN_TTFB_S=60`。阈值依据：p95=21s、
   一个 50.4s 合法离群点、之后断层到 300s。failover 日志带传输失败原因。
2. **探针健康**：传输异常 → `healthy=false` + `probe_failed` 事件 + 按跃迁一行 WARNING；
   三桶共享一个探针 client。
3. **cordon 幂等 + 软 429**：只有状态跃迁才记账；`free_quota_soft` 按 `Retry-After`；
   更短的判决不得覆盖更长的存活 cordon。
4. **前缀审计化**：IP 变化即可解除 IP 域 cordon 并接受轮换结果，不符记 `prefix_mismatch`。
5. **熔断迟滞**：`ZEN_FALLBACK_RECOVER_AFTER=2`；同轮再跳闸记 INFO `FALLBACK RE-ENGAGED`
   （事件带 `re_trip`）。
6. **错误归因**：新增 `errors.InvalidRequest(ValueError)` 为唯一 400 标记
   （`_build_body` / `tools_contract` / `anthropic_proto`），其余 `ValueError` → 500 + ERROR。
7. 附带：免费层 403 触发纳入 30 分钟轮换下限；`_aggregate_sse` 容忍非数值 `created`。

## 3. 验证

| 项 | 结果 |
|---|---|
| `pytest tests/` | 310 passed |
| `pytest balancer/tests/` | 33 passed（合计 345） |
| `ruff check proxy_opencode tests balancer` | All checks passed |
| `mypy proxy_opencode` | no issues（38 files） |
| `egress-agent` 三件套 | 67 passed / 3 skipped, ruff + mypy 干净 |
| `scripts/e2e_protocols.py` | 32/32 |
| 金丝雀（0.7.0a3，关换 IP） | healthz 200；一次真实 chat 200/4.3s，含跨桶 failover |
| 黑洞代理实测（`ZEN_TTFB_S=10`） | 10s 触发 `TtfbTimeout` → 切备用 → 200/16.3s（旧行为 300s+） |
| 本机 hermes 工具调用 e2e | 2/2 通过（`e2e_probe.txt`、`e2e_final.txt` 内容逐字校验） |
| 上线后 10 分钟 | 3×200，0 条 ERROR/WARNING；三桶 healthy 且无 cordon |

## 4. 未解决 / 需决策

1. **上游偶发 400**：`stream=false, tools=false` 形态，24h 1 次，原样透传；三种常见
   请求体复现均为 200，未定位具体触发字段。建议下次出现时带 `x-request-id` 对比。
2. **容量仍受免费层配额约束**：三桶按出口 IP 独立烧穿，换 IP 是当前唯一扩容手段；
   本次修复只消除了「把非配额 429 当配额」与「配额判定造成的容量损失」。
3. **域名入口** `llm.223722.xyz` 仍 404（cloudflared 无 hostname ingress；大陆 ECS
   受 ICP 约束）——需备案或迁非大陆节点。
4. `ZEN_TOOLS_MODE=captured2` 与 AGENTS 默认 `minimal` 不一致（线上显式设置，功能正常）。
