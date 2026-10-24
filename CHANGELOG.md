# Changelog

> 2026-10-03 文档治理说明：早期版本日志中提及的历史文档已合并到 `docs/OPERATIONS.md`/`docs/ACCESS.md` 并移除。当前事实以 `AGENTS.md` 为准。
> 2026-09-30 活体复测修正：免费层门槛为 `stream=true` + 至少两个内置工具名；system marker 并非强制条件。

## [Unreleased]

### Added
- **feat(egress): 第四桶 `win10`（Windows 本机）接入出口池** — 本机只提供代理与数据转发（egress-agent 的 CONNECT 隧道 + `/healthz`），网关编排仍全部在 ECS。部署件：`deploy/windows/start-egress-agent.ps1`（幂等防重 + healthz 自检，Startup 目录 launcher 开机自启），配置在用户目录 `%USERPROFILE%\.egress-agent\win10.json`（凭据不入仓，见 `docs/ACCESS.md`）；listener 只绑 Tailscale IP；无 ONT、不入 `ZEN_EGRESS_ROTATION`。ECS 侧 `.env` 三处接入（PROXIES 追加桶、EXPECT_PREFIX 新桶前缀 `106.111.0.0/16` 族、ROTATION 不列）+ wifi 桶前缀补今晨重拨段。真测：池四桶探针全绿、CONNECT 数据面经本机隧道 opencode.ai 200。注意：win10 与 opc2-wifi 同家庭 WAN（同一公网 IP，上游额度共享、传输路径独立）。文档：`docs/OPERATIONS.md` §1/§2/§0.5、`docs/ARCHITECTURE.md` §1/§2、`deploy/ecs/env.example`。
- **docs: `docs/EGRESS_OBSERVABILITY.md`** — 新增 `egress_request` JSONL 埋点方案（Schema、失败分类、ContextVar+try/finally 埋点建议、logrotate、jq/python 统计示例）。
- **feat(stream): `STREAM_STALL_S` SSE 停顿看门狗配置（默认 60s，0=关闭）** — 所有反代流的静默上限（覆盖响应头后首字节与字节间隔）；`deploy/ecs/env.example` 有注释说明，事实源在 `proxy_opencode/config.py`。
- **feat(stream): `CLIENT_STALL_S` 客户端写停滞看门狗配置（默认 60s，0=关闭）** — `relay_stream` 在 `yield` 停留超阈值即判客户端段死（ASGI 写被背压阻塞），取消响应任务并快速关闭连接；`deploy/ecs/env.example` 有注释说明，事实源在 `proxy_opencode/config.py`。
- **feat(transport): zen 上游一跳改走 bun 传输子进程（2026-10-24 传输指纹门）** — 上游按 TLS/HTTP 栈判定客户端：同一新出口 IP 分钟级对照，bun（boringssl/HTTP2）字节级克隆重放 **200**、httpx（OpenSSL/HTTP1.1）同字节重放 **429**（伪装成配额 `FreeUsageLimitError`）；头级单变量（httpx 自动 `Accept-Encoding`/缺失 `Accept`）均 200 ⇒ 唯一差异=栈。实现：
  - `proxy_opencode/zen_bun/forward.ts`：一次性子进程契约——stdin 请求体、stdout 原始 SSE/错误体直通、stderr 单行 `META {status,headers}`（fetch 收到响应头即写，正文尚未开始）；hop-by-hop 头剥离；`AbortController` 按 `ZEN_BUN_TIMEOUT_MS` 掐断；零日志零本地端口。
  - `upstreams/zen_bun_transport.py`：`BunTransport`（spawn/环境净化/代理注入/超时/kill）+ `_BunResponse`（httpx.Response 形状的鸭子面：`status_code/headers/aiter_bytes/aiter_lines/aclose`）+ `BunTransportError`。
  - `upstreams/zen_direct.py`：`_exchange` 分发器（bun 优先、httpx 回退）+ `_exchange_via_bun`（429/5xx/403-门槛/流式/聚合与 httpx 路径同语义同日志）；流收尾钩子提取为 `_stream_relay`（两传输共用）；`_stream_relay`/`_egress_proxy_url`——后者按当前租得桶 agent 即时拼 Basic 凭据（secret 不入 spec/admin/日志）。
  - `openai_http`：`ResponseLike` Protocol 统一 httpx/bun 响应面（`StreamRelay`/`_aggregate_sse` 类型放宽）。
  - `config.py`：`ZEN_BUN_TRANSPORT/ZEN_BUN_BIN/ZEN_BUN_SCRIPT`（镜像 ENV 默认 =1；dev 无 bun 自动回退 httpx，测试不受影响）。
  - 镜像：`deploy/ecs/Dockerfile` 预装 bun 1.4.4（npmmirror 下载，运行期零外网依赖）+ `COPY zen_bun/forward.ts`；`deploy/ecs/env.example` 注释齐。
  - 测试：`tests/test_zen_bun_transport.py` 13 例（from_settings 门禁、spawn 环境/代理注入、META 协议四态、429/流式适配器路径、凭据拼装、行切分）。
  - 文档：`AGENTS.md` 硬约束 1（新增传输栈门条目+撤回探针烧穿归因）、`docs/OPERATIONS.md` §0.5 第三轮定案、`docs/ARCHITECTURE.md` §1。

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
- **fix(zen): 免费层形状门槛复测=两层门（工具名 ≥4 真名 + 每-IP/UTC日额度），2026-10-24 全池 429 定案** — 今日全池匿名 429 的根因是**两层叠加**并被当日探针自证混淆（字节级证据与消融阶梯见 `docs/OPERATIONS.md` §0.5）：
  - **层 1 形状门（`403 FreeTierError`）**：要求 **≥4 个真名 opencode 工具**（1/2/3 真名全 403；4 真名 200 且 **schema/description 内容不校验**；混入 1 个改名工具即 403）。UA 版本（2.0.18 UA 亦过）/头名/`b3`+`traceparent`/`x-opencode-client` 值*全部不敏感*（消融证）。旧"≥2 内置名 + 合成 schema"口径是 2026-09-30 时代结论，上游已收紧。
  - **层 2 配额门（`429 FreeUsageLimitError`）**：按**出口 IP / UTC 日**计量，`Retry-After` 指向下一个 UTC 0 点（实测 62799s）；传输失败/TLS reset/403 不烧额度。**当日全池 429 的根因：数百发手工/容器探针把桶 IP 与 win10 家宽的当日匿名额度提前烧尽**（网关 `egress_request` 账本 count=0 因不含探针）——"整窗 count=0 却全池 429"由此闭环。
  - 层 1 判定先于层 2：形状缺陷请求在配额已烧穿的 IP 上仍返回 403（相邻请求分获 403/429 实证）。
  - 修复（`zen_direct`+`config`）：`minimal` 注入数 **2→4**（pool 前四位改 1.19.4 客户端实发的 `bash,edit,glob,read`）、`captured2` 同步扩为该 4 工具子集、UA 默认改 `1.19.4/ai-sdk 4.0.23/bun 1.4.4`；头集对齐 1.19.4 抓包——新增 **`x-opencode-session-id`**（与 `x-opencode-session` 同值双头）、移除 `x-session-id`（legacy 稳定 UUID 头，1.19.4 已不发送）与重复的 `x-opencode-request-id`；`x-opencode-project` 由 `"global"` 改固定 40-hex 实例哈希（`OPENCODE_ZEN_PROJECT_ID` 可覆盖）。403 failover/cordon 语义保持不变（防御保留）。
  - 测试：`tests/test_zen_direct.py`、`tests/test_config.py` 同步；pytest 258+33、ruff、mypy 全绿。
  - 文档：`AGENTS.md` 硬约束 1、`docs/ARCHITECTURE.md` §5、`docs/OPERATIONS.md` §0.5。
- **fix(egress): 僵尸桶 ready 语义——agent `ready` 反映探针活性与最近探测结果；网关核实 `egress_probe_ok`（2026-10-24 同日第二缺口）** — 部署首轮修复（上一条）时复测发现：opc2-wifi 的 `/healthz` `ready` 是**硬编码 true**，出口整体死（`bind_source` DHCP 漂移 `192.168.2.138`→`.235` + 电信腿对 `opencode.ai:443` 自身不可达、连败数小时放行），agent 照答 ready=true，池以 ready 单一判据选桶 → 僵尸桶持续领客户端工作（502 燃料）。修复分两层防御：
  - agent `egress_agent/health.py`：`ready = egress_probe_ok is not False and 探针新鲜`——探针环每次尝试（成败皆）打单调点 `probe_at`，`PROBE_READY_GRACE_S=45s`（interval+15s）内视为新鲜；环停转（崩溃/卡死）或最近一次探测失败 → `ready: false`。旧字段 `egress_probe_ok` 保持不变。
  - 网关 `egress/pool._probe_one`：第二道防线——payload `egress_probe_ok is False` 即判 unhealthy（即便 agent 仍答 ready=true）；旧 agent 不带该字段行为不变。
  - 测试：`egress-agent/tests/test_health.py`（失败探测 / 探针环 stale / 首探前 ready=false）、`tests/test_egress_pool.py`（`egress_probe_ok=false` → unhealthy / 显式 true → healthy）。
  - 文档：`docs/ARCHITECTURE.md` §5 ADR、`docs/OPERATIONS.md` §0.5。
- **fix(egress): 配额/门槛 cordon 随 ISP 换 IP 解除 + agent 不可达的轮换继续轮询（hermes `billing` 429，2026-10-24）** — 定位：01:42–01:54Z 三桶连环故障——opc2-eth 硬 429 烧穿每日免费额度且 `POST /rotate` 撞上隧道抖动 30s 超时（`agent unreachable`，而 ONT 实际已在重拨）、opc2-wifi 出口传输连败 cordon、cc9 轮换所得新 IP（CGNAT 共享）同样瞬间 429；兜底耗尽后 429 `Free usage limit reached` 原样透传给 hermes（layer=billing, retryable=false）。修复（两处根因级缺口）：
  - `egress.pool._probe_one → _lift_ip_scoped_cordon`：429 配额烧穿（`free_quota_exhausted`）与 403 门槛标记（`free_tier_rejected`）是**按出口 IP 的判决**——ISP 侧自行重拨得到合法前缀内的新 IP 且探针就绪时，cordon **立即解除**（事件 `cordon_lifted`，failure 计数清零）；外前缀 IP（eth 腿跳到 wifi 出口）与探针未就绪一律不解除；transport 配额类之外的 streak cordon 保持原 deadline，新 IP 若同样 429 会在首个请求上重新 cordon。
  - `egress.rotate._rotate_flow`：`POST {agent}/rotate` 传输失败（可能命令已送达 agent 或 ONT 自行重拨）不再立即判 `failed`——照常在 deadline 内轮询 `/healthz` 观测 IP 变化，观测到合法新 IP 即按成功收敛；明确的 HTTP 拒绝（状态码/`ok=false`）仍是最终失败。
  - 测试：`tests/test_egress_pool.py`（配额 cordon 随 IP 变解除 / 外前缀与未就绪不解除 / transport streak 不解除 / 403 门槛 cordon 解除）、`tests/test_egress_rotation.py`（unreachable 后轮询成功 / unreachable + IP 未见变化仍失败，detail 带两个原因）、`tests/test_egress_eventlog.py`、`tests/test_config.py` 联动放宽成功路径 poll deadline（冷启动探针数百 ms）。
  - 文档：`docs/ARCHITECTURE.md` §5、`docs/OPERATIONS.md` §0.5。
- **fix(ops): 全池 429 根因第三次（最终）修正——传输栈门，探针烧穿归因撤回** — 当日三段递进归因（①探针烧穿配额 → ②CGNAT 共享池"新 IP 到手即被租户烧穿" → ③最终=传输栈判定）以 ②→③ 的双栈对照实验定案：同机同出口 IP 分钟内，bun 字节级克隆 4/4 200、httpx 同克隆重放 429（`FreeUsageLimitError` 原文）；头级单变量排除后唯一差异=TLS/HTTP 栈。据此：层 2"数百发探针烧穿"与"全新 IP 即熄火=CGNAT 共享池"两个早前结论均撤回（429 不烧额度；探针从不触上游）；**探针红线保留**（200 计费的活体调用才消耗）。修复=feat(transport) 一条。已知待办：`egress_request` per-call 账本行在当前构建缺失（事件流仅 cordon/rotation 类），观测性缺口另行修复。详见 `docs/OPERATIONS.md` §0.5 第三轮定案。
- **fix(zen): 每次上游试探重新 mint 会话（上游 session 单次使用，2026-10-24 PM 决定性）** — 上游对**复用会话**一律回层 2 同款 `429 FreeUsageLimitError`：同 IP 分钟级对照实测，新 mint=200、立即重放=429，且该 IP 随后仍连续服务新 mint ≥3 发。旧 `chat()` 每客户端请求 mint 一次、贯穿三条 failover 腿 = 从第二条腿起就是对已消费 session 的重放（这也解释了 bun 部署后首轮聊天仍整链 429：栈门已过但会话三腿衰竭）。修复：`chat()` in-loop 逐试探 `_headers()`；新增回归测试 `test_chat_mints_fresh_session_per_upstream_attempt`（failover 两腿两 session）。E 系消融证据链：E1 假代理被拒连（bun 尊重 `HTTPS_PROXY`，排除直连）；E2/E4/A 同 IP 新会话+克隆 body 200×3（网关自 mint 头亦证无罪）；E7/E8/E9/E10 复用会话 429×4（克隆/最小 body 均无罪）；E5/E6 的 401 为实验脚本用了未映射模型 ID（`_zen_model` 只剥 provider 前缀，别名→裸 ID 映射在 route/catalog 层）。文档：`AGENTS.md` 硬约束 1 第四轮条目、`docs/OPERATIONS.md` §0.5 第四轮定案。
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