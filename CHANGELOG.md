# Changelog

## [0.6.1] - 2026-09-30

### Changed

- **移除网关限流**：删除每 key 60 RPM 固定窗口限速器（`ratelimit.py` 整个
  模块、auth 层 429 分支、`REQUESTS_PER_MINUTE` 配置项）。Zen 上游自带
  调度与配额策略，自建层限流只会惩罚 agent 并发扇出；客户端自行排队。
  LB 侧 `MAX_INFLIGHT`（64）饱和保护保留——它是"跳过过载桶"而非排队。
- **Zen 免费层指纹重校准（提速）**：2026-09-30 活体 ablation 实测，门禁
  **不再校验系统提示词内容**（一句 tiny prompt 甚至无 system 消息均 200），
  仅强制内置工具 schema（无 tools 403；1 个工具 403；`{bash, read}` 两个
  即 200）。新增两个环境变量：
  `ZEN_MARKER_MODE`：`bridge`（默认，仅 ~70 token 工具提示）/ `full`
  （旧版完整 opencode prompt + bridge，~7.8k tokens，可按桶回退）/
  `none`（完全不注入）；
  `ZEN_TOOLS_MODE`：`minimal`（默认，仅 bash+read 两个 schema，~1k
  tokens）/ `all`（完整 11 工具 schema，~6k tokens，按桶回退用）。
  默认组合下每个请求上游 prefill 输入从 ~7.8k 降至 ~1k tokens。

## [0.6.0] - 2026-09-30

### Added

- **模型掩码层（`registry.py`）**：对外只暴露 `ds41f_cus`（DeepSeek V4.1 Flash）
  一个模型；客户端请求任意模型名都透明路由到上游真实模型，且响应（JSON 与
  流式 SSE）中的模型字段一律改写为对外 id——上游模型名不出网关。
  `PUBLIC_MODELS` 可配置目录（`对外id:展示名:上游模型`），`MASK_MODELS=false`
  可整体关闭回退旧行为。
- **SSE 模型字段改写（`sse_mask.py`）**：流式中继逐行改写 `"model"` 字段，
  行缓冲处理跨 TCP chunk 断行的 JSON 载荷。
- **API key 有效期（`auth/` 包）**：新增动态 key 存储（JSON 原子写持久化），
  通过管理接口签发的 key 默认 **24 小时**有效（`KEY_DEFAULT_TTL_HOURS`），
  支持自定义 TTL 与 `ttl_hours: 0` 永久；到期/吊销立即失效并记审计日志。
  `ADMIN_API_KEYS`（默认 `api_ychzh22372222`）永久有效；存量
  `GATEWAY_API_KEYS` 静态 key 保持永久（四桶配置向后兼容）。
- **管理接口**：`POST /admin/keys`（签发）、`GET /admin/keys`（列表，脱敏）、
  `DELETE /admin/keys/{id}`（吊销），仅管理员 key 可用。
- **OpenAI Responses API（`POST /v1/responses`）**：codex CLI 可直连。
  请求侧转换 `instructions`/`input`（字符串或类型化 items）/扁平 function
  tools；响应侧输出完整 Responses 信封；流式按
  `response.created → output_item.added → output_text.delta →
  output_item.done → response.completed` 事件序列下发，含 usage 汇总。
- **Anthropic Messages API（`POST /v1/messages`）**：claude code 可直连。
  支持 `x-api-key` 与 Bearer 两种认证；`system`/content blocks
  （text/tool_use/tool_result）/`input_schema` tools 双向转换；流式按
  `message_start → content_block_start → content_block_delta →
  content_block_stop → message_delta → message_stop` 事件序列下发；
  401/429 错误使用 Anthropic 错误信封。

### Changed

- **模块化细分**：`security.py` 升级为 `auth/` 包（`core.py` 鉴权依赖 +
  `keystore.py` 动态 key 存储）；新增 `registry.py`（模型别名层）、
  `sse_mask.py`（流式改写）、`formats/` 包（`responses_proto.py` /
  `anthropic_proto.py` / `sse_iter.py` 协议转换）；routes 拆分为
  chat / models / responses_api / anthropic_api / admin 五个模块；
  serve 模式响应整形抽取为共享的 `completion_to_chat_response()`。
- healthz 启动日志增加 admin key 数量与对外模型目录。

### Tests

- 测试 49 → 86：新增 registry/掩码（含跨 chunk 断行）、keystore（默认
  24h/永久/过期/吊销/持久化）、admin 接口（鉴权隔离/脱敏）、Responses
  请求与流式事件序列、Anthropic 请求/块转换/流式事件序列/x-api-key 认证、
  两种协议错误信封。
- 端到端真机验证 20/20 通过（真实上游）：三种协议接口、掩码无泄漏、
  key 全生命周期。

## [0.5.1] - 2026-09-29

### Fixed

- **Zen 新会话门禁适配**：匿名请求必须携带稳定 `x-session-id`（UUID）头，
  否则 Zen 返回 401 "Missing API key"。网关启动时自动生成会话 UUID 并随每个
  上游请求发送（`Settings.zen_session_id`，可用 `OPENCODE_ZEN_SESSION_ID`
  固定）。注意：会话 UUID 与 Zen 后端行为存在粘性，遇到持续劣质/异常响应时
  **重启桶进程换新会话** 即可恢复。

### Docs

- `docs/OPERATIONS.md`：脱敏运维手册（架构拓扑、四桶守护与开机自启、
  Zen 协议要点、Hermes 接入、已知问题与 TODO）。

## [0.5.0] - 2026-09-28

### Added

- **balancer：配额感知调度（quota-aware cordon）**：上游桶对请求回 429
  `FreeUsageLimitError`（免费层按出口 IP 日配额耗尽）时，该桶被熔断到下一个
  UTC 零点（北京 08:00）自动解除，期间不再向其派发请求；桶自己的
  `code=rate_limit_exceeded` 429 不触发熔断（靠响应体区分，有单测）。
  全部桶都熔断时把上游真实 429 透传给客户端，而不是伪 502。
- **balancer：request_id 贯通**：每请求生成/继承 `x-request-id`，下发到桶、
  回显给客户端、进入 LB 日志，LB→桶→Zen 全链路可追踪。
- **balancer：`/admin` 管理面板**（`/admin` HTML 自动刷新 5s、`/admin/json`
  机器可读；Bearer 或 `?key=` 鉴权）：每桶健康、配额状态与剩余重置分钟、
  UTC 日请求计数、inflight、失败数、延迟、最后错误一屏可见。
- **balancer：`/healthz` 扩展**：每桶增加 `quota_exhausted` /
  `quota_reset_in_min` / `quota_hits` / `daily_requests` / `daily_date`。
- **`scripts/deploy_node.sh`**：Linux 服务器 / Termux 一键部署（装依赖、
  拉仓库、venv、写 `.env` 与 `run_node.sh`）；`scripts/proxy_opencode.service`
  systemd 单元模板。四桶拓扑（win10-118 / win10-108 / ubuntu-26 / 手机
  Termux-115）统一部署入口。
- **测试**：balancer 10→17 项（配额熔断与到期解封、全桶 429 透传、自身限流
  不熔断、request_id 转发与回显、/admin 鉴权、UTC 日计数），全套 66 绿。

### Fixed

- 端口漂移修正：`docs/dual-bucket-topology.md` 与 `balancer/run.cmd.example`
  的网关端口 8787 全部更正为实际监听的 8791。

## [0.4.0] - 2026-09-28

### Added

- **`balancer/`：双桶轮询负载均衡**（新子项目，`balancer/lb.py`，385 行纯 ASGI）：
  OpenCode Zen 免费额度按**出口 IP** 计，单机多开网关无意义（同一份额度）。
  本组件让多个不同出口 IP 的网关实例合成单一入口：
  - least-connection 轮询，死桶自动跳过（`healthy` 由定时探测维护）；
  - **per-upstream `Authorization` 改写**——各桶密钥不同，故不能用裸
    nginx `proxy_pass`（它做不到 per-upstream 改写 header），这是选 Python
    实现而非 nginx 的硬理由；
  - 客户端 key **不泄漏**到任何后端（`ZEN_LB_API_KEY` 未设置时 fail-closed）；
  - SSE 透传（关缓冲），**仅在未向客户端下发任何字节时**才换桶重试，避免
    重复内容/流拼接错乱；
  - `/healthz` 暴露每桶 `healthy`/`inflight`/`requests`/`failures`/延迟。
  - 配置模板 `balancer/run.cmd.example`（真实 key 版 `run.cmd` 已 gitignore）。
- **`balancer/tests/test_balancer.py`**：10 项单测（全部通过），覆盖
  per-upstream key 改写、客户端 key 不泄漏、死桶 failover、全挂返回规范
  OpenAI 错误体、已开始下发则不换桶等关键契约。
- **CI 新增 `balancer` job**（`.github/workflows/ci.yml`）跑 LB 单测。
- **`docs/dual-bucket-topology.md`**：已验证拓扑、实测容量数据、10 条已知坑
  （工具命名冲突、PowerShell 5.1 限制、日志路径硬编码盘符等）。
- **`docs/roadmap.md`**：正式网关 + 正式对外网站的分阶段路线图与待拍板决策点。

### Fixed

- `balancer/lb.py` 的 `ZEN_LB_LOG` 默认值曾硬编码 `D:\...` 盘符，在无该盘
  的机器上 `os.makedirs` 直接 `FileNotFoundError` 崩溃。改为默认脚本同级
  `logs/lb.log`（相对路径，跨机器可移植）。

### Notes（实测结论，供后续勿走弯路）

- 双桶**不**提升单请求生成速度（单请求 chars/s 实测 -4%）：免费层限流作用
  于**单个请求**，一个长流式请求全程只用一个桶。
- 双桶提升的是**并发吞吐与抗排队**：20 个短请求墙钟 -20%（94s vs 117s）。
- 跨 Tailscale 往返（~400ms）远小于上游生成耗时（~5s），不构成瓶颈。

## [0.3.3] - 2026-09-28

### Added

- **SSE 流生命周期日志**：每条流式请求终止时记录 `stream ended`
  （request_id / model / client / chunks / ttfb_ms / duration_ms / reason）。
  此前流在客户端断开或上游中途断流时会**从日志里无声消失**（既无完成行也
  无错误行），无法区分"慢"与"死了"。reason 取值：`completed` /
  `client_disconnected` / `relay_error:<异常名>`（后者附完整堆栈）。

## [0.3.2] - 2026-09-28

### Fixed

- **`__version__` 与 pyproject 脱节**：包内硬编码的 0.2.0 改为从
  `importlib.metadata` 单一来源读取（未安装时回退 `0.0.0+unknown`）；
  `/healthz` 现在返回 `version` 字段。
- 包 docstring 仍描述 0.2.0 旧架构（"never proxies Zen directly"），已更正。

### Added

- **结构化日志落地**：`chat completion` 的 `extra` 字段（request_id / model /
  stream / has_tools / status / latency_ms / usage / client）此前未被默认
  格式渲染，现已实际输出；新增 `LOG_FORMAT=json` 切换 JSON 行格式
  （`logsetup.py`，字段白名单机制——白名单外的 extra 一律不渲染，杜绝
  误泄敏感数据）。
- **安全审计日志**：401 鉴权失败与 429 限流现在记录 WARNING（client、reason、
  是否提供了凭据；**绝不记录凭据值**）。
- **兜底异常处理**：chat 处理器未预期的异常统一返回 OpenAI 格式 500 并记录
  完整堆栈（仍不含消息内容）。
- **启动日志**：版本 / 模式 / 绑定地址 / key 数量 / 适配器名。

## [0.3.1] - 2026-09-27

### Added

- `HOST` 环境变量（默认 `127.0.0.1`）：设为 `0.0.0.0` 可让局域网设备调用网关。
  安全约束：非回环绑定必须配置 `GATEWAY_API_KEYS`，否则启动即报错（防止把
  无鉴权的 OpenAI 兼容代理暴露到局域网）。
- 一键启动脚本 `start_gateway.bat`：双击即以 `HOST=0.0.0.0`、`PORT=8791`、
  `GATEWAY_API_KEYS=dev-local-key` 启动网关。

## [0.3.0] - 2026-09-27

### Added

- **`zen-direct` 上游模式（新默认）**：直连 OpenCode Zen 的 OpenAI 兼容端点
  （`https://opencode.ai/zen/v1/chat/completions`），完整重构 opencode
  1.18.32 客户端协议：
  - 协议头按真机抓包逐字节复刻（`Authorization: Bearer public`、
    `User-Agent: opencode/<ver> ai-sdk/provider-utils/4.0.23 runtime/bun/<ver>`、
    `x-opencode-client/project/session/request`）。
  - 会话/消息标识符精确复刻 opencode `schema/src/identifier.ts` 算法
    （26 字符 = 12 位 hex 时间部分 + 14 位随机，时间部分与 opencode.db
    中真机数据逐对吻合）。
  - **免费层校验三要素**（消融实测，推翻 0.2.0 的 TLS 指纹结论）：
    ① 系统提示词标记（随包 `zen_prompt_default.txt`）
    ② opencode 内置工具 schema 列表（随包 `zen_builtin_tools.json`，
    11 个 schema 抓包提取；客户端工具合并其后、同名以客户端为准）
    ③ `stream: true`（免费层只服务流式，网关恒流式、非流式客户端聚合）。
  - **真流式**：SSE 逐字节透传（token 级流式、`reasoning_content`、
    原生 `tool_calls`，不再是合成单 chunk）；非流式由 `_aggregate_sse`
    聚合（增量合并 tool_calls、捕获 usage、容忍非标准 cost 尾 chunk）。
  - 配置：`OPENCODE_ZEN_BASE_URL`、`OPENCODE_ZEN_API_KEY`（付费密钥时
    自动免注入标记）、`OPENCODE_ZEN_MODELS`、`OPENCODE_ZEN_TIMEOUT_S`、
    `ZEN_PROXY`、`OPENCODE_ZEN_CLIENT_VERSION`、`OPENCODE_ZEN_BUN_VERSION`。
  - `/v1/models` 直接拉取 Zen 真实模型目录。
- 新测试套件 `test_zen_direct.py`（16 用例：ID 算法与 opencode.db 真机
  配对回归、协议头、标记注入、工具合并、SSE 聚合、错误中继）。
- hermes 端到端验收通过：普通问答、写文件工具循环（2 tool calls 落盘）、
  多轮 ls+read 工具循环（3 tool calls）；模型正确调用客户端自定义工具。

### Changed

- 默认 `UPSTREAM_MODE` 从 `opencode-serve` 改为 `zen-direct`。
- `opencode-serve` 模式保留作为无代理环境下的回退。

### Notes（背景结论，2026-09-27 实测）

- 免费层校验三要素：系统提示词标记 + 内置工具 schema + stream=true
  （消融验证：任一缺失 → 403 `FreeTierError`；内置+客户端工具合并 → 200）。
- 免费额度按出口 IP 计（429 `FreeUsageLimitError`），按 UTC 日重置。
- hermes e2e 验收见 `scripts/e2e_hermes.md`。

## [0.2.0] - 2026-09-26

### Changed（架构重构，行为刻意收敛）

- **拆分单体 `app.py`** 为模块：`errors.py`、`security.py`、`ratelimit.py`、
  `routes/`（chat、models）、`upstreams/`（协议 + `openai_http` +
  `opencode_serve` + `tools_contract`）。`app.py` 只剩装配。
- **移除 `opencode-cli` 模式**：subprocess 桥接在本机实测不可靠且能力受限
  （无流式/无工具），serve 模式全面代之。
- `UPSTREAM_MODE` 默认从 `openai` 改为 `opencode-serve`。
- serve 适配器重写为**当前 opencode 1.18.x 的 v2 `/api` 协议**
  （create session -> prompt -> 轮询 message -> 删除临时 session）；废弃旧版
  `/wait`（空闲时也返回 503）与旧 payload 形态（`{"prompt": {...}}` 包裹）。

### Added

- **工具调用桥接**：外部 OpenAI `tools` 经 JSON 契约注入 prompt，回复解析回
  `tool_calls`（`metadata.tools_source=json-contract-bridge`）；`tool_choice`
  支持 `required`/指定函数。
- **`reasoning_effort`** 被接受并映射为建议性提示（low/high/max）。
- assistant 的 reasoning part 映射为 `reasoning_content`；opencode 内部工具
  调用记录到 `metadata.internal_tools`。
- 合成流式额外返回 `usage` 不透明字段之外保持 OpenAI chunk 形态，
  并带 `X-Opencode-Serve-Synthetic-Stream` 头。
- 新测试套件：`test_tools_contract.py`、`test_serve_adapter.py`、
  `test_gateway_routes.py`、`test_config.py`（21 用例）。
- `AGENTS.md`、`docs/engineering-constraints.md`、`scripts/e2e_hermes.md`、
  `scripts/e2e_hermes.py`（hermes 36 条真实请求驱动脚本）。

### Notes（背景结论，已被 0.3.0 修正）

当时认为 Zen 免费层是"客户端指纹校验"（结论有误：真实机制是请求体标记 +
出口区域 + 客户端指纹的组合，见 0.3.0），本网关因此只桥接本机官方 serve。
0.3.0 已实现合规边界内的直连方案。

## [0.1.0] - 2026-09-24

- 初始版本：OpenAI 透传 / opencode-cli / opencode-serve 三模式单体网关。
