# proxy_opencode

OpenAI 兼容网关。默认 **`zen-direct`** 直连 [OpenCode Zen](https://opencode.ai/)
（镜像 opencode 1.19.4 客户端协议），支持免费层模型、原生 tool call、token 级流式。

```text
hermes / codex CLI / claude code / 任意 OpenAI SDK
  ──OpenAI API──> proxy_opencode ──协议直连──> OpenCode Zen
```

生产部署是 **ECS 单容器 + 多设备出口池**（每桶一个公网出口 IP，即免费层计量
维度），拓扑与端口见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 模式

- **`zen-direct`（默认）**：按 opencode 1.19.4 客户端协议直连
  `https://opencode.ai/zen/v1/chat/completions`。免费层有多层门（形状 / 传输栈 /
  session / body / 模型 / 配额），事实见 [`AGENTS.md`](AGENTS.md) 硬约束 1。
  SSE 逐字节透传，非流式由网关聚合。
- **`opencode-serve`（回退）**：驱动本机官方 opencode serve API。
- **`openai`（透传）**：通用 OpenAI 兼容上游透传。

## 快速开始（本地开发）

```bash
# Linux/macOS（Windows PowerShell 用 $env:）
export GATEWAY_API_KEYS=dev-local-key
python -m proxy_opencode          # 默认 127.0.0.1:8787

curl http://127.0.0.1:8787/healthz
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/v1/models
curl -H "Authorization: Bearer dev-local-key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-ai/deepseek-v4.1-flash","messages":[{"role":"user","content":"1+1=?"}]}' \
  http://127.0.0.1:8787/v1/chat/completions
```

> 本地无 bun 时自动回退 httpx；**生产必须走 bun**（传输栈门）。

## 对外接口

| 接口 | 客户端 | 认证 |
|---|---|---|
| `POST /v1/chat/completions` | 任意 OpenAI SDK / hermes | `Authorization: Bearer <key>` |
| `POST /v1/responses` | codex CLI（Responses API） | `Authorization: Bearer <key>` |
| `POST /v1/messages` | claude code（Anthropic Messages） | `x-api-key: <key>` 或 Bearer |

公开别名 `deepseek-ai/deepseek-v4.1-flash` 由 `PUBLIC_MODELS` 第三列决定上游
（生产当前 `big-pickle`，`ZEN_MODEL_MAP` 可覆盖）。**仅公开别名可请求**，上游裸
ID 一律 404；裸 ID 只通过隐藏端点 `GET /v1/models_cus?key=<key>` 展示（排障）。

## 开发与门禁

```bash
uv venv --python 3.12.13 .venv
uv pip install -e '.[test,dev]' --python .venv/Scripts/python.exe
pip install -e 'egress-agent[test,dev]'

# 全绿才算完成（完整要求见 AGENTS.md 硬约束 3）
pytest tests/ -q && pytest balancer/tests/ -q
ruff check proxy_opencode tests balancer && mypy proxy_opencode
cd egress-agent && pytest tests/ -q && ruff check egress_agent tests && mypy egress_agent
```

## 文档地图

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **改代码前必读**：协议事实与安全红线 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 拓扑、端口注册表、出口池语义、ONT 驱动、ADR |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | 部署现状、常用操作、排障顺序、风险与待办 |
| [`docs/ACCESS.md`](docs/ACCESS.md) | 入口、凭据名称与去向（不含值） |
| [`docs/EGRESS_OBSERVABILITY.md`](docs/EGRESS_OBSERVABILITY.md) | 事件流现状 + 逐请求账本规格与统计示例 |
| [`docs/SELF_OPTIMIZATION.md`](docs/SELF_OPTIMIZATION.md) | 自我优化闭环：观测契约、诊断规则、执行分级、proxy_ms 兜底保证 |
| [`docs/engineering-constraints.md`](docs/engineering-constraints.md) | 工程化基线 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本历史 |

## 安全原则

- **日志红线**：绝不记录消息内容、prompt 或任何 key（含 httpx 访问日志）。
- **凭据不入仓库**：真实密钥/口令只在仓库外，文档只记去向。
- **回环限定**：关键上游 URL 强制校验（loopback / `opencode.ai`）。
