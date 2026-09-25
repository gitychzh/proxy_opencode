# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)，
变更记录格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Added

- 工程化基线：GitHub Actions CI（Python 3.12.13 + pytest）、`CONTRIBUTING.md`、
  `.editorconfig`、`.nvmrc`，以及 ruff/black/mypy 的最小配置。
- `UPSTREAM_MODE=opencode-cli` 本机官方 CLI 上游模式（受限、仅本机调试）：
  `/v1/models` 走 `opencode models`（allow prefix 过滤 + TTL 缓存），
  chat 走 `opencode run`（list-form subprocess + 超时 + 禁 shell），
  stream 合成单 chunk 并标注 `synthetic_stream`，不支持 tools/思考透传
  （显式 400），CLI 失败返回 502。新增 `scripts/hermes_opencode_local.md`。
  默认 `openai` HTTP 模式不变；不含任何风控绕过/额度放大/客户端伪造。
- `UPSTREAM_MODE=opencode-serve` 本机官方 `opencode serve` HTTP 上游模式
  （仅 loopback、Basic auth、密码为空只允许 127.0.0.1）：chat 走
  session→prompt→wait→message，reasoning 合入 `reasoning_content`，tool
  parts 只读映射为 `tool_calls`（`tools_source=opencode-agent`），stream 为
  合成单 chunk，客户端 tools/reasoning 字段显式 400，连接失败 502、超时
  504。`/v1/models` 使用 `OPENCODE_SERVE_MODELS` 静态列表兜底。新增
  `scripts/hermes_opencode_serve.md` 与基于本地 FastAPI fake serve 的测试。
  免费额度/限制由官方服务端控制，不做任何 bypass。

## [0.1.0] - 2026-09-25

### Added

- OpenAI-compatible 反向代理网关：转发 `GET /v1/models` 与 `POST /v1/chat/completions`
  到 `UPSTREAM_BASE_URL`，支持 stream 与非 stream 透传。
- 网关侧鉴权（`GATEWAY_API_KEYS`，空为开发模式）与内存限流（`REQUESTS_PER_MINUTE`）。
- 请求字段白名单透传，含可开关的 reasoning 字段（`REASONING_PASSTHROUGH`）。
- 元数据请求日志（request_id/model/status/latency/usage），不记录消息体与密钥。
- `GET /healthz` 健康检查。
- 初始 pytest 测试与 `docs/engineering-constraints.md` 工程化约束。

[Unreleased]: https://github.com/gitychzh/proxy_opencode/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gitychzh/proxy_opencode/releases/tag/v0.1.0
