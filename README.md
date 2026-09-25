# proxy_opencode

OpenAI 兼容的本机网关：把 [hermes](https://github.com/NousResearch/hermes-agent)
等任意 OpenAI SDK 客户端，桥接到**本机官方 `opencode serve`**，从而使用
OpenCode Zen 的免费模型（如 `opencode/big-pickle`，支持 tool call + reasoning）。

```
hermes / OpenAI SDK ──OpenAI API──> proxy_opencode ──loopback HTTP──> opencode serve ──> Zen
```

## 为什么不经本网关直连 Zen？

实测（2026-09）：免费层在服务端做客户端指纹校验，非 opencode 客户端
（curl/httpx/python，即使复刻 UA 与 x-opencode-* 头）一律返回
`FreeTierError: OpenCode's free tier can only be used from within OpenCode`。
因此本项目**不**直连、不伪造指纹——而是驱动本机官方 `opencode serve`。
它本来就是一个真正的 opencode 客户端，免费额度、限流完全由官方服务端执行。

## 快速开始

```bash
# 1. 起官方 serve（回环，强密码）
set OPENCODE_SERVER_PASSWORD=选一个强密码        # bash: export ...
opencode serve --hostname 127.0.0.1 --port 4096

# 2. 起网关（默认 UPSTREAM_MODE=opencode-serve）
set UPSTREAM_MODE=opencode-serve
set OPENCODE_SERVE_URL=http://127.0.0.1:4096
set OPENCODE_SERVER_PASSWORD=与上一致
set GATEWAY_API_KEYS=dev-local-key             # 不设则为 dev-open（仅本机调试用）
python -m proxy_opencode                       # 默认 127.0.0.1:8787
# 或: uvicorn proxy_opencode.app:app --host 127.0.0.1 --port 8787
```

验证：

```bash
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/healthz
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/v1/models
curl -H "Authorization: Bearer dev-local-key" -H "Content-Type: application/json" -d @- http://127.0.0.1:8787/v1/chat/completions <<'EOF'
{"model":"opencode/big-pickle","messages":[{"role":"user","content":"用一句话回答 1+1=?"}]}
EOF
```

hermes 配置（`config.yaml` 的 `providers:` 下加一条）：

```yaml
providers:
  proxyo:
    name: proxyo
    base_url: http://127.0.0.1:8787/v1
    model: opencode/big-pickle
    api_key: dev-local-key
    discover_models: false
    api_mode: chat_completions
```

## 语义与边界

- **端点**：`GET /healthz`（无需鉴权）、`GET /v1/models`、
  `POST /v1/chat/completions`（非流式 + 合成流式）。
- **字段白名单**：仅透传 OpenAI 标准字段（`messages`/`tools`/`temperature`/…，
  见 `upstreams/fields.py`）；`REASONING_PASSTHROUGH=true` 时含 reasoning 字段。
- **工具调用**：serve 的 prompt API 只收文本；外部 tools 通过 JSON 契约桥接，
  `tool_calls` 双向如实映射（`metadata.tools_source=json-contract-bridge`）。
  模型代理内部使用 opencode 自带工具时，在 `metadata.internal_tools` 标注。
- **思考强度**：`reasoning_effort` 接受并映射为指令提示（low→简短思考，
  high/max→深度思考），属建议性提示，模型遵守程度以实际为准。
- **流式**：serve 无 token 级流式，`stream=true` 返回合成单 chunk 并带
  `metadata.synthetic_stream=true` 与响应头 `X-Opencode-Serve-Synthetic-Stream`。
- **错误**：连接不上 serve → 502；超时 → 504；鉴权 → 401；限流 → 429。
- **无状态**：每个 chat 请求映射为一个**临时 opencode session**，完成后删除
  （可用 `OPENCODE_SERVE_EPHEMERAL_SESSIONS=false` 关闭）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UPSTREAM_MODE` | `opencode-serve` | `opencode-serve`（本机官方 serve）或 `openai`（通用透传） |
| `OPENCODE_SERVE_URL` | `http://127.0.0.1:4096` | **仅允许 loopback**；无密码时只允许 127.0.0.1 |
| `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | `opencode` / 空 | serve 的 Basic auth |
| `OPENCODE_SERVE_MODELS` | `opencode/big-pickle` | `/v1/models` 暴露的模型列表（逗号分隔） |
| `OPENCODE_SERVE_TIMEOUT_S` | `120` | serve 单次 HTTP 请求超时 |
| `OPENCODE_SERVE_WAIT_TIMEOUT_S` | `600` | 单回合等待完成超时 |
| `GATEWAY_API_KEYS` | 空（dev-open） | 网关 Bearer key（逗号分隔） |
| `REQUESTS_PER_MINUTE` | `60` | 每个网关 key 的限流 |
| `PORT` | `8787` | 网关端口 |
| `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` | `https://api.openai.com` / 空 | 仅 `openai` 透传模式 |

`openai` 透传模式：把白名单字段原样转发到任意 OpenAI 兼容端点，SSE 流
原样透传，上游错误按原状态码与 JSON 返回。

## 日志

每次 chat 记录：`request_id`、`model`、`stream`、`has_tools`、`status`、
`latency_ms`、`usage`。**绝不记录消息内容与任何 key。**

## 测试与质量

```bash
pytest tests/ -q                    # 单元 + 路由测试（respx 伪 serve，CI 可跑）
python scripts/e2e_hermes.py        # 端到端：本机 hermes 36 条真实请求
```

端到端与 drive 细节见 `scripts/e2e_hermes.md`。
CI：`.github/workflows/ci.yml`（Python 3.12.13 + pytest + ruff）。

## 仓库治理

- `AGENTS.md`：维护约定与架构说明（**改动前先读**）
- `CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`
- `docs/engineering-constraints.md`：工程化基线
