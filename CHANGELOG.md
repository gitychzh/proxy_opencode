# Changelog

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

### Notes（背景结论）

Zen 免费层有客户端指纹校验（非 opencode 客户端一律 `FreeTierError`），
本网关因此只桥接本机官方 serve，不直连、不伪装。

## [0.1.0] - 2026-09-24

- 初始版本：OpenAI 透传 / opencode-cli / opencode-serve 三模式单体网关。
