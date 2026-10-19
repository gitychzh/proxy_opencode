# Changelog

> 2026-10-03 文档治理说明：早期版本日志中提及的历史文档已合并到 `docs/OPERATIONS.md`/`docs/ACCESS.md` 并移除。当前事实以 `AGENTS.md` 为准。
> 2026-09-30 活体复测修正：免费层门槛为 `stream=true` + 至少两个内置工具名；system marker 并非强制条件。

## [Unreleased]

### Added
- **docs: `docs/EGRESS_OBSERVABILITY.md`** — 新增 `egress_request` JSONL 埋点方案（Schema、失败分类、ContextVar+try/finally 埋点建议、logrotate、jq/python 统计示例）。
- **feat(stream): `STREAM_STALL_S` SSE 停顿看门狗配置（默认 60s，0=关闭）** — 所有反代流的静默上限（覆盖响应头后首字节与字节间隔）；`deploy/ecs/env.example` 有注释说明，事实源在 `proxy_opencode/config.py`。
- **feat(stream): `CLIENT_STALL_S` 客户端写停滞看门狗配置（默认 60s，0=关闭）** — `relay_stream` 在 `yield` 停留超阈值即判客户端段死（ASGI 写被背压阻塞），取消响应任务并快速关闭连接；`deploy/ecs/env.example` 有注释说明，事实源在 `proxy_opencode/config.py`。

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
- **fix(stream): SSE 停顿看门狗 + egress 租约按结局释放（hermes `stream_drop`，2026-10-19）** — 定位：故障在网关→上游段——opc2-wifi 死隧道令流零字节静默满 `OPENCODE_ZEN_TIMEOUT_S=300`，`httpx.ReadTimeout` 穿透 `StreamRelay` 后 uvicorn 掐断客户端 SSE，hermes 报 `stream_drop`（完整复盘见 `docs/OPERATIONS.md` §0.2）。修复：
  - `openai_http.StreamRelay`：停顿看门狗（`STREAM_STALL_S`）超时或中途 httpx 传输异常 → 记 WARNING（`reason=stream_stalled`/`stream_interrupted`，只含错误类与耗时，无内容）→ 走既有合成终止块路径收尾（`finish_reason:"length"` + `[DONE]`），异常不再穿透掐断连接；关闭钩子改携带结局（completed/cut/stalled/interrupted/disconnected）。
  - `zen_direct`：按结局释放租赁——`stalled`/`interrupted` 按**失败**（`kind=stream_stalled`/`stream_interrupted`，3 连败 cordon 死桶）并记 WARNING 带 `egress=桶名`；此前 `_released` 恒 `ok=True`，坏桶永不累计失败、永不 cordon。`cut`（上游干净截断，健康隧道仅转发）与客户端断连仍按 ok 释放（带 kind 供统计）。
  - 测试：`tests/test_zen_direct.py`（停顿/首字节静默/中途中断/看门狗开启下的正常完成/关闭钩子结局/池按失败释放）、`tests/test_config.py`（默认/覆盖/关闭）；文档：`docs/ARCHITECTURE.md` §5 ADR、`docs/OPERATIONS.md` §0.2、`deploy/ecs/env.example`。
- **fix(stream): 客户端段写停滞看门狗（hermes 600s `stream_drop`，2026-10-19）** — 定位：故障在 **hermes↔网关最后一跳**（公网段黑洞），与 §0.2 的上游段静默不同——网关侧上游仍在正常产块，`relay_stream` 已中继 242 块但卡在 uvicorn `send()`（背压 `flow.drain()`），既无上游静默也无 TCP 断连可观测，直到 hermes 600s 自杀（12:04:59Z `Stream stale for 600s → WinError 10053`）；caddy `aborting` 长期计数证明该客户端段劣化是慢性问题，非本次回归（完整复盘见 `docs/OPERATIONS.md` §0.4）。修复：
  - `routes/_pipeline.py::relay_stream`：`client_stall_s` 参数 + 监视任务。生成器停在 `yield` 恰等于 ASGI 写该块被背压阻塞，"交出块后超阈值没来取下一块"即判客户端段死（仅上游读期间不清零，慢上游不误报）→ WARNING `client write stalled`（含 request_id/model/client/chunks/blocked_s，无内容）、结束理由 `client_stalled`、取消响应任务（uvicorn 随即 `transport.close()`，客户端秒级失败重试）。监视任务**自行**释放上游并写终态 `stream ended`——被取消的响应任务经其 `CancelledError` traceback 钉住生成器（`owner` 闭包成环），依赖生成器 close 会把释放与日志拖到环状 GC 之后。
  - 三路由（`routes/chat.py`、`routes/responses_api.py`、`routes/anthropic_api.py`）传入 `settings.client_stall_s`。
  - 测试：`tests/test_logging.py`（停滞触发 WARNING+`client_stalled`+取消任务+释放上游+不记完成、`0` 关闭仍可完成、慢上游不误报）、`tests/test_config.py`（默认/覆盖/关闭）；文档：`docs/ARCHITECTURE.md` §5 ADR、`docs/OPERATIONS.md` §0.4、`deploy/ecs/env.example`。
- **fix(deploy): 容器启动补 `setup_logging()`（生产日志曾整层丢失）** — Dockerfile CMD 走 `uvicorn --factory proxy_opencode.app:create_app_from_env`，从未经过 `python -m proxy_opencode`，容器根 logger 无 handler：全部 INFO（`chat completion`/`stream ended`）被丢弃、WARNING 走 lastResort 无格式无 extras，**无法从日志归因到桶**（2026-10-19 排障被迫翻设备 journalctl）。现 `create_app_from_env()` 先调 `setup_logging()`（uvicorn 默认 LOGGING_CONFIG 无 `root` 键，顺序安全）；测试 `tests/test_logging.py::test_app_factory_configures_logging`。
- **fix(zen): 兜底耗尽的匿名 403 `FreeTierError` 改回 503 `egress_unavailable`（不再透传成客户端 auth 错）** — 2026-10-19 06:39Z 实测：单请求烧穿全部 3 次尝试（cc9 被标记 403 → opc2-wifi 隧道传输失败 → opc2-eth 被标记 403），分类 / cordon / 轮换全部正常，但兜底把**出口级** 403 原样透传，hermes 判为 layer=auth、retryable=false；约 90s 后轮换已恢复全池（复测 200）。修复：`zen_direct` 新增 `_egress_gate_answer`——最终（兜底耗尽或无池直连）的匿名 403 `FreeTierError` → **503 + `error.code=egress_unavailable`**（WARNING `answering 503`），非门槛 403 与带 key 模式仍按请求级原样透传。测试：`tests/test_zen_direct.py`（兜底 503、无池 503、单桶 cordon+503）；文档：`docs/ARCHITECTURE.md` §5、`docs/OPERATIONS.md` §0.3。

### Docs
- **docs: 同步 0.7.x 三桶栈现状** — 同步 ONT 驱动（huawei_ct/fenghuo）、opc2 策略路由（uid1001→table410@wlx、uid996→table991@enp12s0）、凭据去向约定与可观测性设计。
- **docs: 同步出口桶实况（2026-10-17 部署后）** — `docs/OPERATIONS.md` 拓扑块与桶表更新为当前出口 IP（opc2-wifi `114.239.48.176`、opc2-eth `223.68.24.71`、cc9 `122.195.202.2`）与逐桶 `expect_prefix`；补入轮换后落到的新 ISP 段 `opc2-wifi += 114.239.0.0/16`、`cc9 += 122.195.0.0/16`（否则轮换校验 `_wait_for_ip_change` 必判失败）。