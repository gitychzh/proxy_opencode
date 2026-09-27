# Changelog

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
