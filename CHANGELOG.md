# Changelog

## [0.6.3] - 2026-10-02

第二轮系统性审计（安全 + 协议正确性 + 上游漂移）。所有修复均带回归测试；
网关 159 → 165 项、LB 33 项、协议级 e2e 32 项全绿；ruff 与 mypy 干净。

### Security — 公开仓库凭据清零（严重）

- **真实凭据从仓库移除**：`docs/ACCESS.md` 曾包含云账号 AccessKey、
  Cloudflare Global API Key、ECS/SWAS root 密码与本机密码；
  `config.py` / `balancer/lb.py` 曾硬编码真实默认 admin key；CONTRIBUTING
  的"私有仓库明文例外"前提不成立（仓库实为 PUBLIC）。全部改为占位符 +
  "去仓库外 `scripts_local/secrets.env` 取"，新增 §9 凭据轮换清单。
- **CI 守卫 `tests/test_no_secrets.py`**：结构性正则扫描全部 git 跟踪文件，
  拦截 `LTAI…` / `cfk_…` / `api_<lower><digits>` / `gw-lb-…` / 私钥块等。
- **默认凭据 fail-closed**：网关 `ADMIN_API_KEYS` 默认改为本地开发占位符
  `dev-admin-key` 且非回环绑定时强制显式配置；LB `ZEN_LB_ADMIN_KEYS` 默认
  改为**空**（原默认值使 fail-closed 守卫与 503 分支沦为死代码），
  `ZEN_LB_API_KEY` 未设置时 LB 拒绝一切请求。
- **恒定时间密钥比较**：静态 key 集合与动态 keystore 的比对全部改用
  `hmac.compare_digest`。

### Fixed — 逻辑与协议正确性

- **`/admin/keys` 的 `ttl_hours: NaN` 铸出永久 key（严重）**：NaN 与任何
  数值比较均为 False，绕过 `ttl < 0` 与 `effective_ttl > 0` 两道判断 →
  `expires_at=None`。`Infinity` 则使 `timedelta(hours=inf)` 抛
  `OverflowError` → 500。现统一要求有限数（`math.isfinite`），keystore 侧
  非有限 TTL 回落默认 24h 兜底。
- **上游模型 ID 去前缀漂移（2026-10-02 实测）**：Zen 目录改用裸 ID
  （`big-pickle`），旧拼写 `opencode/big-pickle` 401 `ModelError`。
  zen-direct 默认映射同步改为裸 ID；`opencode-serve`（本地 opencode 命名
  空间）保持 `provider/model` 不变。
- **真实网关全链路 e2e 通过 + 429 误判澄清（2026-10-02）**：额度恢复后
  以真实 zen-direct 网关完成端到端验证（非流式 / 流式 / 工具调用全 200，
  掩码与 usage 正确，`get_weather` 参数正确回传）。此前把本机直连出口的
  429 误读为"额度耗尽"的泛化结论已纠正：429 按出口 IP / UTC 日独立计量，
  UTC 0 点重置后同探测恢复 200；连发 6 次、直连/代理双路径均 200，无突发
  限流。另确认直连 opencode.ai 存在间歇性 SSL EOF，探测需带重试——新增
  诊断工具 `scripts/diag_zen_429.py`（多出口路径对比 + 响应头取证）。
- **Anthropic 流式 tool_use 块惰性开启**：`content_block_start` 是工具名
  唯一可发布点，旧实现收到首个 delta 立即开块，晚到/分片的 `function.name`
  只保留首段甚至为空，客户端拿到无名工具调用。现累积名字、在首个参数增量
  到达时才开块，流结束兜底冲刷未开启槽位。
- **流式 usage 抓取跨 chunk 失效**：`StreamRelay` 逐网络 chunk 做
  `json.loads`，`data:` 行被 TCP 分片切开即静默丢弃。改为行缓冲扫描，
  字节仍逐字透传。
- **三协议统一流式生命周期日志**：`/v1/responses`、`/v1/messages` 的流式
  分支此前在流开始前就记 200（latency 只到首字节、无 usage、中途失败无
  日志）。`_relay` 上移为 `_pipeline.relay_stream`，三条路由共用；客户端
  断连时显式 `aclose()` 内层生成器，及时释放上游连接。
- **错误中继体过掩码**：上游 4xx/5xx 错误体原样回传可能夹带上游模型名，
  现与成功体同样过 `mask_json_model`。
- **SSE 泄漏名独立键形态**：`{"name":"Space Bunny"}`（无相邻逗号）此前
  两种逗号锚定的正则都匹配不到，现兜底清空值。
- **`GET /v1/models` 异常面收窄**：适配器意外异常不再裸 500，统一 502
  信封。
- **e2e 断言修正**：`preserves tool function names` 检查原先跑在未带
  `tools` 的请求上（场景不成立恒失败），现改在带工具的流式请求上验证
  `function.name` 存活且泄漏名被清。

### Tests

- 新增回归：NaN/Infinity TTL（路由层 + keystore 层）、Anthropic 工具名
  晚到/分片累积、双工具调用独立块索引、泄漏名独立键、恒定时间比较下的
  key 生命周期、LB 无 key fail-closed、客户端断连不计熔断、payload dump
  关闭断言锚定真实目录、限流突发测试真并发化（asyncio.gather）、
  caplog logger 名修正（`proxy_opencode.auth`）。

## [0.6.2] - 2026-09-30

一轮系统性缺陷排查与工程化重构（逻辑 / 代码 / 部署 / 使用四面）。所有修复
均带回归测试；网关 92 → 108 项、LB 23 → 28 项，全绿；ruff 与 mypy 首次
真正纳入 CI 门禁并全绿。

### Fixed — 逻辑与协议正确性

- **Responses 流式 `output_index` 冲突（严重）**：`/v1/responses` 流式中，
  assistant 消息项固定占 `output_index=0`，而工具调用项也从 0 开始编号，
  两者同时出现时**索引重复**，客户端会把工具调用挂到消息项上；且流内索引
  与最终 `response.completed` 信封的 `output` 顺序不一致。改为单一自增
  计数器为所有 output item 分配唯一索引，`final_output()` 按索引排序。
- **Responses 流式补齐函数调用增量事件**：新增
  `response.function_call_arguments.delta` / `.done`，此前只在
  `output_item.done` 里一次性给出完整参数，codex 等按增量拼装的客户端
  会拿到空参数。
- **Anthropic SSE 事件体缺 `type` 字段**：数据体只含 `index`/`delta` 等，
  而 Anthropic 线格式要求 `data` 内重复事件名（SDK 依赖 `data["type"]`
  分发）。现所有事件数据体均带 `"type": "<event>"`。
- **Anthropic 空 assistant 轮次（严重）**：仅含 `thinking` 块或 `content: []`
  的 assistant 消息被转成 `{"role":"assistant","content":null}` 原样上送，
  上游整单 400。现直接丢弃此类空轮次；全部轮次皆空时报 400 客户端错误。
- **`content_block_stop` 事件体规范化**：移除多余 `content_block` 字段，
  仅保留 `index`（对齐 Anthropic 规范）。
- **zen-direct 空消息清洗**：`_clean_messages` 现在丢弃既无 `content` 也无
  `tool_calls` 的轮次（reasoning-only 消息等），全部被丢弃时抛 `ValueError`
  → 网关返回规范 400，而不是把必然 400 的请求打到上游。
- **`opencode-serve` 去掉 `assert`**：`assert result is not None` 在 `-O`
  下被剥离；改为显式 `ServeError`。

### Fixed — 健壮性与运维

- **keystore 非对象 JSON 崩溃（严重）**：`keys.json` 若为 JSON 数组等非对象
  结构，`data.get("keys")` 抛 `AttributeError` 未被捕获，**网关在导入期直接
  崩溃无法启动**。现校验顶层结构并跳过畸形记录，`expires_at` 非字符串时按
  “无过期”处理。LB 侧 `EdgeKeyStore` 同缺陷同修。
- **balancer 全桶饱和误返 502（严重）**：所有桶 `inflight >= MAX_INFLIGHT`
  时，跳过逻辑把每个桶都 skip 掉，请求以“全部上游不可用”502 结束。现仅在
  **存在空闲桶**时才跳过饱和桶，否则照常尝试（让上游排队）。
- **balancer `/admin/keys` 方法回落**：PUT/PATCH 等未支持方法此前会穿透到
  代理路径被打到桶上，现返回 405；非法 JSON 请求体此前被当作空体静默签发
  24h key，现返回 400。
- **admin 接口错误码**：`POST /admin/keys` 非法 `ttl_hours`、
  `DELETE /admin/keys/{id}` 未知 id 此前返回 **HTTP 200 + error 体**，
  客户端必须解析响应体才知道失败。现分别为 400 / 404，并使用统一
  OpenAI 错误信封。
- **导入期副作用移除**：`proxy_opencode.app` 不再在导入时构造应用（原先会
  创建 httpx 客户端、读 `keys.json`、校验环境）。`python -m proxy_opencode`
  改为构造一次应用对象交给 uvicorn，`load_settings()` 不再执行两次（此前
  匿名会话 UUID 会被生成两次）。新增
  `uvicorn --factory proxy_opencode.app:create_app_from_env`。
- **日志白名单缺字段**：`key_id` / `key_name` / `ttl_hours` / `path` /
  `admin_keys` / `public_models` 等作为 `extra` 传入却不在白名单，被两个
  formatter **静默丢弃**（密钥生命周期与存储告警实际不可见）。已补齐。
- **`PUBLIC_MODELS` 解析丢条目**：展示名含冒号的目录项此前被静默丢弃并回落
  到内置目录；现按“首段=id、末段=上游、中间=展示名”解析，畸形条目单独跳过。

### Fixed — 四桶 + 客户端全链路实测新增（2026-09-30）

- **SSE 泄漏上游模型展示名（严重）**：Zen 在 `delta.name` 里回填上游模型的
  人类可读名（实测 `{"role":"assistant","content":"OK","name":"Space Bunny"}`）。
  掩码层只改写顶层 `model` 字段，于是 `Space Bunny` 随流式响应泄漏给客户端，
  “上游模型名不出网关”的设计目标被绕过。现按“工具/函数名不可能含空白字符”
  的判据，在字节层剔除带空白的 `delta.name`（连同分隔逗号，保持 JSON 合法）；
  非流式路径的 `message.name` 同样处理。
- **LB 配额熔断过于激进（严重）**：任一上游 429 即把该桶熔断到 **UTC 零点**
  （最长 ~24h）。但 Zen 的 429 文案是 “Rate limit exceeded. Please try again
  later.”，实测同一桶 429 后 **5 分钟内即恢复 200**——却仍被排除在调度之外
  约 24h，静默损失 1/4 容量。现改为**冷却窗口**（`ZEN_LB_QUOTA_COOLDOWN`，
  默认 900s，自动重探），且永不越过当日重置点；`0` 恢复旧的“熔断到重置”。
- **`scripts/e2e_hermes.py` 的 toolset 名错误**：`-t files` 并非合法
  toolset（正确是单数 `file`）。hermes 会打印 “Unknown toolset: files” 后
  **加载 0 个工具**，而模型仍会礼貌作答——工具类用例因此长期“看似通过”。
  现修正为 `-t terminal -t file`，并新增**落盘断言**（工具用例必须真的产出
  文件，否则整轮判失败），让这类静默失效无法再被掩盖。

### Fixed — 出口路径不再跟随环境代理（2026-10-01 实测）

- **`trust_env` 默认关闭**：四个 httpx 客户端（zen-direct / openai 透传 /
  balancer）此前都用 httpx 默认的 `trust_env=True`，会跟随
  `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 与 Windows 注册表代理。实测
  win10-local 桶因此**持续 502**：上游调用在 ~2.0s 内
  `ConnectError: All connection attempts failed`，而同一台机器的网络完全正常
  （`curl --noproxy` 与 `trust_env=False` 均 200）。复现实验确认：带上
  `HTTP_PROXY=http://127.0.0.1:7897`（已失效的 Clash）后，失败耗时 2.3s，与
  线上观测的 2.0s 吻合。
  现新增 `UPSTREAM_TRUST_ENV`（默认 `false`）统一控制四个客户端；需要走代理
  请用显式的 `ZEN_PROXY`，不要让出口路径依赖机器的环境变量状态。

### Changed — 提示词瘦身（2026-09-30 抓包实测，本轮重点）

对 claude code / hermes / codex 三个客户端做了真实抓包，定位输入 token 的
实际构成并裁剪。**端到端输入 token 降低约 57%**（claude code：21.1k → 8.9k）。

- **网关注入精简（省 ~1,900 tok/请求，全客户端生效）**：活体消融（ubuntu26
  出口，可复现）确认免费层门禁的**真实规则**——它只要求**至少 2 个名字属于
  opencode 内置工具集**的工具，**完全不校验 schema 内容**：
  `bash+read` 全量 schema(7,872 字符) → 200；`bash+read` 空参数(246 字符) →
  200；仅名字(104 字符) → 200；**只有 1 个工具 → 403**；**两个非内置名 →
  403**；`bash+read+edit` → 200。
  于是 `ZEN_TOOLS_MODE=minimal`（默认）改为注入两个**合成的最小 schema**
  （~60 token，原 ~1,970）。新增 `captured2`（旧的 bash+read 捕获 schema，
  ~2k token）作为中间回退档，`all` 保持全量 11 工具。注入时跳过客户端已占用
  的名字——旧实现会在同名时丢掉内置工具，可能把数量压到 2 个以下而 403。
- **claude code 工具裁剪（省 ~9,500 tok/请求，降幅 50%）**：抓包显示其请求中
  **23 个工具定义占 60,272 字符（~15,068 tok，79%）**，其中
  DesignSync/SendMessage/Workflow/ScheduleWakeup/Cron*/Worktree*/
  ReportFindings/ListAgents 共 11 个与编码无关。已通过 `settings.json` 的
  `permissions.deny` 摘除（Claude Code 会把这些工具**从请求中移除**，而非仅
  拒绝执行）：请求体 76,027 → 37,740 字符，工具 23 → 11（保留 Agent/Bash/
  Edit/Glob/Grep/NotebookEdit/Read/Skill/WebFetch/WebSearch/Write）。
  工具链回归：Write+Read 落盘验证通过。

### Changed — 工程化 / 模块化

- **新增 `routes/_pipeline.py`（共享请求管线）**：`/v1/chat/completions`、
  `/v1/responses`、`/v1/messages` 三条路由原本各自复制了“解析 → 模型别名 →
  字段白名单 → 调用适配器 → 异常映射 → 日志”约 60% 的代码，导致缺陷同步
  复制（serve 分支重复记录同一条完成日志）与错误映射三处维护。现统一收敛，
  三条路由只保留各自的**请求转换**与**响应整形**。
- **修复重复日志**：`/v1/responses`、`/v1/messages` 在 serve 模式非流式分支
  会把同一次完成记录两遍；管线化后仅记录一次。
- **balancer `create_app` 结构化**：单函数 260 余行拆为
  `_handle_admin_keys` / `_handle_admin_views` / `_proxy` 三个职责单一的处理
  器，主 `app()` 退化为可读的分发器；顺带修掉 `any_ok` 的冗余恒等表达式。
- **清理死代码**：`zen_direct.timestamps` / `openai_http.timestamps` /
  `registry.build_registry` 删除；`sse_mask.mask_json_model` 改为在 chat 路由
  实际使用（不再游离）。`upstreams/__init__.py` 的协议文档与返回类型更正
  （原文档提到的 `PassThroughStream` 并不存在）。
- **`scripts/e2e_hermes.py` 可移植**：hermes 路径、工作目录、报告路径、模型
  与条数全部改为环境变量可覆盖（`E2E_*`），默认从 `PATH` 找 hermes；找不到
  时快速失败并给出提示，不再硬编码 `C:\Users\...`。

### CI / 工程基线

- **lint 与类型门禁此前形同虚设**：CI 的 ruff 步骤以“ruff 已安装”为前提，
  但 `.[test]` 并不含 ruff，**从未真正执行过**。现新增 `[project.optional-dependencies].dev`
  （ruff + mypy），CI 无条件执行 `ruff check proxy_opencode tests balancer`
  与 `mypy proxy_opencode`（release workflow 同样执行）。
- `pyproject.toml` 版本 0.6.1 → 0.6.2；mypy 全绿（修复 2 处类型错误）。

### Tests

- 新增回归：Responses 输出索引唯一性与顺序一致性、函数调用增量事件、
  Anthropic 空轮次丢弃 / 全空报错 / `content_block_stop` 形状、
  admin 400/404 契约、keystore 异形 JSON 与畸形记录、日志新白名单字段、
  `PUBLIC_MODELS` 含冒号、zen-direct 空消息清洗、LB 全桶饱和仍转发 /
  饱和跳过 / 405 / 400 / 边缘 keystore 异形 JSON。

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
  `ADMIN_API_KEYS`（默认 `dev-admin-key` 本地占位）永久有效；存量
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
