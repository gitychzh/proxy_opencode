# proxy_opencode

OpenAI 兼容本机网关。默认以 **`zen-direct`** 模式直连 [OpenCode Zen](https://opencode.ai/)，支持免费模型（如 `big-pickle`）、原生 tool call 与 token 级流式。

```text
hermes / codex CLI / claude code / 任意 OpenAI SDK
  ──OpenAI API──> proxy_opencode ──协议重构直连──> OpenCode Zen
```

## 模式

- **`zen-direct`（默认）**：按 opencode 1.18.32 客户端协议直连 `https://opencode.ai/zen/v1/chat/completions`。SSE 逐字节透传，非流式由网关聚合。
- **`opencode-serve`（回退）**：驱动本机官方 opencode serve API（当前机器上不可用）。
- **`openai`（透传）**：通用 OpenAI 兼容上游透传。

## 快速开始

```bash
# Linux/macOS（Windows PowerShell 用 $env:）
export GATEWAY_API_KEYS=dev-local-key
python -m proxy_opencode  # 默认 127.0.0.1:8787

# 健康检查（无需鉴权）
curl http://127.0.0.1:8787/healthz

# 验证
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/v1/models
curl -H "Authorization: Bearer dev-local-key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-ai/deepseek-v4.1-flash","messages":[{"role":"user","content":"1+1=?"}]}' \
  http://127.0.0.1:8787/v1/chat/completions
```

## 对外接口

| 接口 | 客户端 | 认证方式 |
|---|---|---|
| `POST /v1/chat/completions` | 任意 OpenAI SDK / hermes | `Authorization: Bearer <key>` |
| `POST /v1/responses` | **codex CLI**（OpenAI Responses API） | `Authorization: Bearer <key>` |
| `POST /v1/messages` | **claude code**（Anthropic Messages API） | `x-api-key: <key>` 或 `Authorization: Bearer <key>` |

默认公开别名：`deepseek-ai/deepseek-v4.1-flash`（映射到上游裸 ID `big-pickle`）。**仅公开别名可被请求**，上游裸 ID 一律 404。被反代裸 ID 仅通过隐藏端点 `GET /v1/models_cus?key=<key>` 展示（排障专用）。

## 本地开发

```bash
uv venv --python 3.12.13 .venv
uv pip install -e '.[test,dev]' --python .venv/Scripts/python.exe

# 核心门禁
pytest tests/ -q
pytest balancer/tests/ -q
ruff check proxy_opencode tests balancer
mypy proxy_opencode

# egress-agent 侧门禁
cd egress-agent
pip install -e '.[test,dev]'
pytest tests/ -q
ruff check egress_agent tests
mypy egress_agent
cd ..
```

## 文档地图

- [`AGENTS.md`](AGENTS.md) — **改代码前必读**：协议事实与安全红线
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 0.7.x 新栈权威（拓扑、端口、ONT 驱动、策略路由、Egress 健康判定）
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — 部署、运行状态、排障、日常操作
- [`docs/ACCESS.md`](docs/ACCESS.md) — 访问入口、API Key 与凭据去向（不含凭据值）
- [`docs/EGRESS_OBSERVABILITY.md`](docs/EGRESS_OBSERVABILITY.md) — `egress_request` JSONL 埋点、失败分类与统计示例
- [`docs/engineering-constraints.md`](docs/engineering-constraints.md) — 工程化基线
- [`CHANGELOG.md`](CHANGELOG.md) — 版本历史

## 安全原则

- **日志红线**：绝不记录消息内容、prompt 或任何 key。
- **凭据不入仓库**：真实密钥/口令只存放在仓库外（设备侧配置文件或 gitignored 本地文件），文档只记去向不记值。
- **回环限定**：关键上游 URL 强制校验（loopback/opencode.ai 限定）。