# Changelog

> 2026-10-03 文档治理说明：早期版本日志中提及的历史文档已合并到 `docs/OPERATIONS.md`/`docs/ACCESS.md` 并移除。当前事实以 `AGENTS.md` 为准。
> 2026-09-30 活体复测修正：免费层门槛为 `stream=true` + 至少两个内置工具名；system marker 并非强制条件。

## [Unreleased]

### Added
- **docs: `docs/EGRESS_OBSERVABILITY.md`** — 新增 `egress_request` JSONL 埋点方案（Schema、失败分类、ContextVar+try/finally 埋点建议、logrotate、jq/python 统计示例）。

### Changed
- **docs: 项目文档整体梳理与去重** — 按“同一事实只在一处维护”原则重构文档体系：
  - `docs/ARCHITECTURE.md`：统一拓扑、端口注册表、ONT 驱动（三驱动 huawei/huawei_ct/fenghuo）、opc2 策略路由、Egress 健康判定（仅依赖探针）。
  - `docs/OPERATIONS.md`：收束至部署、运行状态、排障、日常操作；删除大量历史实测复盘与重复细节，新增日志统计入口指向观测性文档。
  - `docs/ACCESS.md`：收束至访问入口、API Key 与凭据去向（仅索引、不含凭据值），强化“凭据不入仓库”原则。
  - `README.md`：大幅浓缩，突出文档地图与安全原则，去除冗余历史细节。
  - `AGENTS.md`：精简引用链（将冗长架构/协议细节指向 `docs/ARCHITECTURE.md`），强化硬约束与日志红线。
- **docs: 明确 Egress 健康判定** — 全文清理“IP 前缀硬门/expect_prefix 阻断健康”过时表述。健康判定现为 **`healthy = (ready and HTTP 200)`（仅依赖探针就绪）**；`matches_prefix` 仅作审计字段，不再影响 `healthy` 状态。
- **docs: 文档间交叉引用统一** — 建立清晰引用关系，减少重复，提升可维护性。

### Removed
- **docs: 去除冗余段落** — 删除双桶性能对比、抓包复盘等多处重复的历史细节，避免旧拓扑/过期结论干扰当前事实。

### Fixed
- **fix(egress): 免费层 403 `FreeTierError` 按出口 IP 处理（failover + cordon + 换 IP）** — 2026-10-17 定位：协议已合规（≥2 内置工具 + descending session + `stream`）的请求在**被标记的出口 IP** 上仍 403 `FreeTierError`；同一字节请求换新 IP（旋转所得）或 ECS 宿主 IP 即 200；而 `/healthz` 探针不检测门槛，被标记的桶仍判健康并持续分发。修复：
  - 适配器 `zen_direct`：匿名模式 403 `FreeTierError` → 释放租赁为 `free_tier_rejected`（失败），并触发 `chat()` 桶间 failover（SSE 未开播、客户端零字节）；非匿名或其它错误类型的 403 仍按请求级 ok 释放。
  - 池 `pool.release`：`free_tier_rejected` **立即 cordon**（不等 3 连败），并通知 observer。
  - 轮换 `rotate` + `counters.should_rotate_on_free_tier`：**绕过阈值立即换 IP**，每窗口每桶至多一次，失败按 `ROTATION_RETRY_COOLDOWN_S` 冷却后可重试（与硬 429 共用去重/冷却）。
  - 适配器 `_headers()`：恢复抓包中的匿名 `Authorization: Bearer public`（与模块文档一致；实测该头有无均可 200）。
  - 测试：`test_zen_direct.py`（failover / cordon / 非门槛 403 / 带 key 不换桶）、`test_egress_pool.py`（立即 cordon + 跳过）、`test_egress_rotation.py`（门槛一次性轮换）。
  - 文档：`AGENTS.md` 硬约束 1、`docs/ARCHITECTURE.md` §5、`docs/OPERATIONS.md` §0.1。

### Docs
- **docs: 同步 0.7.x 三桶栈现状** — 同步 ONT 驱动（huawei_ct/fenghuo）、opc2 策略路由（uid1001→table410@wlx、uid996→table991@enp12s0）、凭据去向约定与可观测性设计。
- **docs: 同步出口桶实况（2026-10-17 部署后）** — `docs/OPERATIONS.md` 拓扑块与桶表更新为当前出口 IP（opc2-wifi `114.239.48.176`、opc2-eth `223.68.24.71`、cc9 `122.195.202.2`）与逐桶 `expect_prefix`；补入轮换后落到的新 ISP 段 `opc2-wifi += 114.239.0.0/16`、`cc9 += 122.195.0.0/16`（否则轮换校验 `_wait_for_ip_change` 必判失败）。