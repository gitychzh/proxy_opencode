# proxy_opencode

OpenAI 兼容的本机网关：把 [hermes](https://github.com/NousResearch/hermes-agent)
等任意 OpenAI SDK 客户端，直连 **OpenCode Zen**，使用其免费模型
（如 `big-pickle`，支持 token 级流式 + 原生 tool call + reasoning）。

```
hermes / OpenAI SDK ──OpenAI API──> proxy_opencode ──重构协议直连──> OpenCode Zen
```

## zen-direct 直连模式（默认）

`UPSTREAM_MODE=zen-direct` 会按 opencode 1.18.32 客户端的**真实协议**直连
`https://opencode.ai/zen/v1/chat/completions`：

- **协议头**：按真机抓包逐字节复刻（`Bearer public` 匿名认证、
  `User-Agent: opencode/<ver> ai-sdk/... runtime/bun/<ver>`、
  `x-opencode-client/project/session/request`，会话/消息 ID 用 opencode
  同款标识符算法实时生成）。
- **免费层校验（最近实测）**：匿名请求需要 `stream: true`，并带至少两个
  OpenCode 内置工具名；工具 schema 可用默认注入的最小版本。最近复测中，tiny prompt
  或没有 system 消息也通过，因此不再把 system marker 列为强制门槛。网关恒流式调 Zen，
  非流式客户端由网关聚合 SSE。完整实测边界与复核纪律见 `AGENTS.md`。
- **出口路由**：由运行网关的主机决定。默认不跟随环境代理
  （`UPSTREAM_TRUST_ENV=false`）；需要代理时用 `ZEN_PROXY` 显式配置。公网/CDN 的
  IPv4、IPv6 和路由质量因设备与 ISP 而异，不能从一台设备的结果推断所有节点。
- **真流式**：SSE 逐字节透传——token 级流式、`reasoning_content`、原生
  `tool_calls`（不需要 serve 模式的 JSON 契约桥接与合成流）。
- **配额**：免费额度按出口 IP 计、按 UTC 日重置；共享 VPN 出口可能 429
  （`FreeUsageLimitError`），网关按原状态码如实中继，换 IP 或次日重试即可。
- 配置付费 `OPENCODE_ZEN_API_KEY` 后自动免注入标记（按量计费路径）。

## 快速开始

```bash
# Linux/macOS（Windows PowerShell 请改用 $env:GATEWAY_API_KEYS="dev-local-key"）
export GATEWAY_API_KEYS=dev-local-key
python -m proxy_opencode                       # 默认 127.0.0.1:8787

# 另一个终端验证；健康检查不需要鉴权
curl http://127.0.0.1:8787/healthz
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/v1/models
curl -H "Authorization: Bearer dev-local-key" -H "Content-Type: application/json" \
  -d '{"model":"ds41f_cus","messages":[{"role":"user","content":"用一句话回答 1+1=?"}]}' \
  http://127.0.0.1:8787/v1/chat/completions
```

## 三种协议接口（v0.6.0 起）

对外只暴露一个模型 `ds41f_cus`（DeepSeek V4.1 Flash），客户端请求任意模型名
都会透明路由到它，且响应（JSON 与流式 SSE）中的模型字段一律改写为
`ds41f_cus`——上游真实模型名不出网关。

| 接口 | 客户端 | 认证方式 |
| --- | --- | --- |
| `POST /v1/chat/completions` | 任意 OpenAI SDK / hermes | `Authorization: Bearer <key>` |
| `POST /v1/responses` | **codex CLI**（OpenAI Responses API） | `Authorization: Bearer <key>` |
| `POST /v1/messages` | **claude code**（Anthropic Messages API） | `x-api-key: <key>` 或 Bearer |

codex CLI 配置（`~/.codex/config.toml`）：

```toml
[model_providers.gateway]
name = "gateway"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[model_providers.gateway.env_key]
name = "GATEWAY_API_KEY"
```

claude code 配置（环境变量）：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=dev-local-key
```

API key 管理（管理员 key 永久有效；新生成 key 默认 24 小时有效）：

```bash
# 签发（默认 24h；ttl_hours: 0 = 永久）
curl -X POST http://127.0.0.1:8787/admin/keys \
  -H "Authorization: Bearer dev-admin-key" -H "Content-Type: application/json" \
  -d '{"name":"my-phone","ttl_hours":24}'
# 列表（脱敏）
curl -H "Authorization: Bearer dev-admin-key" http://127.0.0.1:8787/admin/keys
# 吊销
curl -X DELETE http://127.0.0.1:8787/admin/keys/<key_id> -H "Authorization: Bearer dev-admin-key"
```

Hermes 配置示例（CLI 支持 `hermes config set <key> <value>`；仅用于本机开发）：

```yaml
model:
  default: ds41f_cus
  provider: custom
  base_url: http://127.0.0.1:8787/v1
  api_key: dev-local-key
providers:
  proxyo:
    name: proxyo
    base_url: http://127.0.0.1:8787/v1
    api_key: dev-local-key
    default_model: ds41f_cus
    transport: openai_chat
```

生产密钥须在仓库外管理，不能照抄开发用占位符。

## 多出口桶池与公开 LB

Zen 免费额度按**出口 IP**计，因此 LB 将多个不同出口的 `proxy_opencode` 网关汇聚到一个
OpenAI 兼容入口。公网有两个入口：Cloudflare Tunnel（`https://llm.223722.xyz/v1`，通用）与
ECS 上的 **IP 直连**（`https://115.29.231.25:9443/v1`，无 SNI，移动网更稳）——后者为何必须用
IP 而不能用域名，见 `docs/ACCESS.md` §7。两者都转到杭州 ECS 上的 Python ASGI LB，再经
Tailscale 分发到四个桶；具体节点状态、运行方式和已知风险统一维护在 `docs/OPERATIONS.md`
与 `docs/ACCESS.md`。

LB 为每个上游单独改写 `Authorization`，支持 least-connection 调度、失败前的重试、
SSE 透传及按桶配额冷却。健康检查当前包含上游明细；公开入口因此存在内部拓扑信息
可见的风险，详见运维手册。

## 语义与边界

- **端点**：`GET /healthz`（无需鉴权）、`GET /v1/models`、
  `POST /v1/chat/completions`（zen-direct 真 SSE 流式；非流式聚合）。
- **字段白名单**：仅透传 OpenAI 标准字段（`messages`/`tools`/`temperature`/…，
  见 `upstreams/fields.py`）；`REASONING_PASSTHROUGH=true` 时含 reasoning 字段。
- **工具调用**：直连模式下 `tools` 原生透传，模型返回原生 `tool_calls`
  （`finish_reason=tool_calls`），hermes 等 agent 客户端零改造。
- **思考**：big-pickle 的 reasoning 以 `reasoning_content` 透传（流式为
  `delta.reasoning_content`）。
- **流式**：`stream=true` 为真 SSE；`stream=false` 由网关聚合完整 JSON。
- **错误**：上游错误按原状态码与 JSON 中继（403 FreeTierError / 429 上游限流 /
  5xx）；连接失败 → 502；网关鉴权 → 401。本地网关不再实施逐 key 限流。
- **无状态**：每个请求一个新 `ses_`/`msg_` 标识符，无服务端会话状态。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UPSTREAM_MODE` | `zen-direct` | `zen-direct`（直连 Zen）/ `opencode-serve`（本机官方 serve）/ `openai`（通用透传） |
| `OPENCODE_ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | Zen API 基址（仅 opencode.ai / loopback） |
| `OPENCODE_ZEN_API_KEY` | 空 | Zen API 密钥；空 = 匿名免费层（`Bearer public` + 标记注入 + 内置工具合并 + 强制流式） |
| `OPENCODE_ZEN_MODELS` | `big-pickle` | `/v1/models` 兜底模型列表（逗号分隔；正常情况直接拉 Zen 目录）。注意：Zen 目录自 2026-10-02 起改用**无前缀**裸 ID |
| `OPENCODE_ZEN_TIMEOUT_S` | `300` | 上游单请求超时 |
| `ZEN_PROXY` | 空 | 显式出口代理（如 `http://127.0.0.1:7897`）。空 = **直连**，不跟随环境代理（见 `UPSTREAM_TRUST_ENV`） |
| `UPSTREAM_TRUST_ENV` | `false` | 是否让出站 httpx 客户端跟随 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 与 Windows 注册表代理。默认关闭：实测失效的代理环境变量会让桶在 2s 内 ConnectError 502。需要代理请用 `ZEN_PROXY` |
| `OPENCODE_ZEN_SESSION_ID` | 空（进程内随机 UUID） | 匿名免费层必需的 `x-session-id` 头；固定它可复现同一上游会话行为 |
| `OPENCODE_ZEN_CLIENT_VERSION` | `1.18.32` | UA 中的 opencode 版本 |
| `OPENCODE_ZEN_BUN_VERSION` | `1.3.14` | UA 中的 bun 版本 |
| `OPENCODE_SERVE_URL` | `http://127.0.0.1:4096` | 仅 serve 模式；**只允许 loopback** |
| `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | `opencode` / 空 | serve 模式的 Basic auth |
| `OPENCODE_SERVE_MODELS` | `opencode/big-pickle` | serve 模式 `/v1/models` 列表 |
| `OPENCODE_SERVE_TIMEOUT_S` | `120` | serve 模式单次 HTTP 调用超时 |
| `OPENCODE_SERVE_WAIT_TIMEOUT_S` | `600` | serve 模式单个 agent 回合的最长等待（超时 504） |
| `OPENCODE_SERVE_EPHEMERAL_SESSIONS` | `true` | 回合结束后删除临时 opencode 会话 |
| `GATEWAY_API_KEYS` | 空（dev-open） | 网关 Bearer key（逗号分隔，**永久有效**） |
| `ADMIN_API_KEYS` | `dev-admin-key`（本地占位，**生产必须改**） | 管理员 key（永久有效，可管理 `/admin/keys`，也可直接调用对话接口） |
| `KEY_STORE_PATH` | `keys.json` | 动态 key 存储文件（JSON，原子写） |
| `KEY_DEFAULT_TTL_HOURS` | `24` | 新生成 key 的默认有效期（小时）；`/admin/keys` 传 `ttl_hours: 0` 可签发永久 key |
| `MASK_MODELS` | `true` | 模型掩码开关；开启后对外只暴露 `PUBLIC_MODELS` 目录，上游模型名从所有响应中抹除 |
| `PUBLIC_MODELS` | `ds41f_cus:DeepSeek V4.1 Flash:big-pickle` | 对外模型目录，格式 `对外id:展示名:上游模型`（逗号分隔多条）。上游 ID 需与 Zen 实时目录一致 |
| `LOG_FORMAT` | `text` | 日志格式：`text`（人类可读）/ `json`（JSON 行，便于采集归档） |
| `PAYLOAD_DUMP_DIR` | 空（关闭） | 调试用：把每个**原始客户端请求体**（含 prompt）落盘到该目录，每协议一个 JSON，仅保留最近 200 个。⚠️ 绕过"不记录消息内容"红线，**生产桶请保持关闭** |
| `ZEN_MARKER_MODE` | `bridge` | 给模型的兼容提示：`bridge`（默认，短工具提示）/ `full`（历史完整 opencode prompt）/ `none`（不注入）。最近复测不要求 system marker；此开关不是门槛保证 |
| `ZEN_TOOLS_MODE` | `minimal` | 工具注入：`minimal`（默认，两项最小 schema）、`captured2`（bash+read 捕获 schema）、`all`（完整 11 工具）；最近复测门槛按工具名/数量校验，见 AGENTS.md |
| `HOST` | `127.0.0.1` | 绑定地址；设 `0.0.0.0` 供局域网调用时**必须**同时设置 `GATEWAY_API_KEYS` 与 `ADMIN_API_KEYS`（内置 `dev-admin-key` 是公开占位符，不可用于对外绑定） |
| `PORT` | `8787` | 网关端口 |
| `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` | `https://api.openai.com` / 空 | 仅 `openai` 透传模式 |

## 日志

每次 chat 记录：`request_id`、`model`、`stream`、`has_tools`、`status`、
`latency_ms`、`usage`、`client`（来源 IP）。401 鉴权失败与上游 429 记录
WARNING 审计日志（只记是否提供了凭据，**绝不记录凭据值**）；chat 的未预期
异常记录完整堆栈并统一返回 OpenAI 格式 500；启动时记录版本 / 模式 / 绑定
地址 / key 数量。**绝不记录消息内容与任何 key。**

```bash
LOG_FORMAT=json python -m proxy_opencode   # JSON 行格式（机器解析/长期归档）
```

日志字段采用白名单机制（`logsetup.py`），白名单外的 extra 一律不渲染。
流式请求终止时记录 `stream ended`（chunks / ttfb_ms / duration_ms / reason：
`completed` / `client_disconnected` / `relay_error:<异常名>`），慢与死可区分。

## 测试与质量

```bash
pytest tests/ -q                    # 单元 + 路由测试（respx 伪上游，CI 可跑）
pytest balancer/tests/ -q           # LB 单测（含 failover / key 改写 / 配额熔断）
ruff check proxy_opencode tests balancer   # 静态检查（CI 强制）
mypy proxy_opencode                 # 类型检查（CI 强制）
python scripts/e2e_hermes.py        # 端到端：本机 hermes 真实请求
```

端到端与 drive 细节见 `scripts/e2e_hermes.md`（路径与条数可用 `E2E_*`
环境变量覆盖，不再硬编码机器路径）。
CI：`.github/workflows/ci.yml`（Python 3.12.13 + ruff + mypy + pytest；
`test` 与 `balancer` 两个 job）。

直接交给 ASGI 服务器时用工厂入口（不会在导入期构造应用）：

```bash
uvicorn --factory proxy_opencode.app:create_app_from_env --host 127.0.0.1 --port 8787
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| `AGENTS.md` | 项目架构、开发约定、协议事实与安全红线（**改代码前必读**） |
| `docs/OPERATIONS.md` | 当前部署状态、操作命令、排障证据与有效待办 |
| `docs/ACCESS.md` | 设备地址、访问方式、凭据存放位置（不含真实凭据） |
| `docs/engineering-constraints.md` | 工程化基线与合规红线 |
| `CHANGELOG.md` / `CONTRIBUTING.md` / `SECURITY.md` | 版本变更 / 协作 / 安全策略 |

`balancer/run.cmd.example` 是可移植的 Windows LB 模板；复制为忽略跟踪的 `run.cmd`
后在设备本地填入密钥，勿将实际配置提交到公开仓库。
