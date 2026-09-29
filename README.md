# proxy_opencode

OpenAI 兼容的本机网关：把 [hermes](https://github.com/NousResearch/hermes-agent)
等任意 OpenAI SDK 客户端，直连 **OpenCode Zen**，使用其免费模型
（如 `opencode/big-pickle`，支持 token 级流式 + 原生 tool call + reasoning）。

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
- **免费层校验（三要素，消融实测）**：服务端对匿名请求做请求体指纹校验，
  三者缺一不可，任一缺失 → 403 `FreeTierError`：
  ① opencode 真实默认系统提示词（随包资产 `zen_prompt_default.txt`）；
  ② opencode 内置工具 schema 列表（11 个，随包资产 `zen_builtin_tools.json`，
  与客户端 tools 合并上送，同名碰撞客户端优先）；③ `stream: true`
  （免费层只服务流式，网关恒流式调 Zen，非流式客户端由网关聚合 SSE）。
- **出口路由**：CN 本地直连即可（与 opencode 官方客户端同一环境同一结论）。
  `httpx trust_env=True` 会自动使用系统代理（Windows 注册表代理 / 环境变量）；
  也可用 `ZEN_PROXY` 显式指定。
- **真流式**：SSE 逐字节透传——token 级流式、`reasoning_content`、原生
  `tool_calls`（不需要 serve 模式的 JSON 契约桥接与合成流）。
- **配额**：免费额度按出口 IP 计、按 UTC 日重置；共享 VPN 出口可能 429
  （`FreeUsageLimitError`），网关按原状态码如实中继，换 IP 或次日重试即可。
- 配置付费 `OPENCODE_ZEN_API_KEY` 后自动免注入标记（按量计费路径）。

## 快速开始

```bash
# 1. 起网关（默认 UPSTREAM_MODE=zen-direct）
set GATEWAY_API_KEYS=dev-local-key             # 不设则为 dev-open（仅本机调试用）
python -m proxy_opencode                       # 默认 127.0.0.1:8787（本地直连即可）

# 2. 验证
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/healthz
curl -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/v1/models
curl -H "Authorization: Bearer dev-local-key" -H "Content-Type: application/json" -d @- http://127.0.0.1:8787/v1/chat/completions <<'EOF'
{"model":"ds41f_cus","messages":[{"role":"user","content":"用一句话回答 1+1=?"}]}
EOF
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
set ANTHROPIC_BASE_URL=http://127.0.0.1:8787
set ANTHROPIC_API_KEY=dev-local-key
```

API key 管理（管理员 key 永久有效；新生成 key 默认 24 小时有效）：

```bash
# 签发（默认 24h；ttl_hours: 0 = 永久）
curl -X POST http://127.0.0.1:8787/admin/keys \
  -H "Authorization: Bearer api_ychzh22372222" -H "Content-Type: application/json" \
  -d '{"name":"my-phone","ttl_hours":24}'
# 列表（脱敏）
curl -H "Authorization: Bearer api_ychzh22372222" http://127.0.0.1:8787/admin/keys
# 吊销
curl -X DELETE http://127.0.0.1:8787/admin/keys/<key_id> -H "Authorization: Bearer api_ychzh22372222"
```

hermes 配置（`config.yaml` 的 `providers:` 下加一条）：

```yaml
providers:
  proxyo:
    name: proxyo
    base_url: http://127.0.0.1:8787/v1
    model: ds41f_cus
    api_key: dev-local-key
    discover_models: false
    api_mode: chat_completions
```

## 双桶轮询：把多份额度合成一个入口

Zen 免费额度**按出口 IP 计**——同一台机器上跑两个网关只是同一份额度。
`balancer/` 让多个**不同出口 IP** 的网关实例合成单一入口：

```
hermes ──> LB :7892 (least-connection 轮询) ──┬──> 网关 A（出口 IP 1）:8787
                                            └──> 网关 B（出口 IP 2）:8787
```

- **per-upstream `Authorization` 改写**：各桶密钥不同。裸 nginx
  `proxy_pass` 做不到 per-upstream 改 header，这是选 Python 实现而非 nginx
  的硬理由。
- **死桶自动 failover**：实测单桶宕机期间客户端请求全部成功，流量自动压到
  另一桶；恢复后自动回池。
- **SSE 透传**；仅在未向客户端下发任何字节时才换桶重试，避免流拼接错乱。
- **`/healthz`** 暴露每桶 `healthy` / `inflight` / `requests` / `failures` / 延迟。

```bash
# 配置：复制模板并填入各桶的真实 key（run.cmd 已被 .gitignore 排除）
cp balancer/run.cmd.example balancer/run.cmd
python balancer/lb.py
```

**实测收益**：并发吞吐 +20%（20 短请求墙钟 94s vs 117s），单桶宕机无感。
**但单请求生成速度不变**（-4%）——免费层限流作用于单个请求。
完整拓扑、实测数据与已知坑见 `docs/dual-bucket-topology.md`。

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
- **错误**：上游错误按原状态码与 JSON 中继（403 FreeTierError / 429 限流 /
  5xx）；连接失败 → 502；网关鉴权 → 401；网关限流 → 429。
- **无状态**：每个请求一个新 `ses_`/`msg_` 标识符，无服务端会话状态。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UPSTREAM_MODE` | `zen-direct` | `zen-direct`（直连 Zen）/ `opencode-serve`（本机官方 serve）/ `openai`（通用透传） |
| `OPENCODE_ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | Zen API 基址（仅 opencode.ai / loopback） |
| `OPENCODE_ZEN_API_KEY` | 空 | Zen API 密钥；空 = 匿名免费层（`Bearer public` + 标记注入 + 内置工具合并 + 强制流式） |
| `OPENCODE_ZEN_MODELS` | `opencode/big-pickle` | `/v1/models` 兜底模型列表（逗号分隔；正常情况直接拉 Zen 目录） |
| `OPENCODE_ZEN_TIMEOUT_S` | `300` | 上游单请求超时 |
| `ZEN_PROXY` | 空 | 显式出口代理（如 `http://127.0.0.1:7897`）；空 = 跟随系统/环境代理 |
| `OPENCODE_ZEN_CLIENT_VERSION` | `1.18.32` | UA 中的 opencode 版本 |
| `OPENCODE_ZEN_BUN_VERSION` | `1.3.14` | UA 中的 bun 版本 |
| `OPENCODE_SERVE_URL` | `http://127.0.0.1:4096` | 仅 serve 模式；**只允许 loopback** |
| `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | `opencode` / 空 | serve 模式的 Basic auth |
| `OPENCODE_SERVE_MODELS` | `opencode/big-pickle` | serve 模式 `/v1/models` 列表 |
| `GATEWAY_API_KEYS` | 空（dev-open） | 网关 Bearer key（逗号分隔，**永久有效**） |
| `ADMIN_API_KEYS` | `api_ychzh22372222` | 管理员 key（永久有效，可管理 `/admin/keys`，也可直接调用对话接口） |
| `KEY_STORE_PATH` | `keys.json` | 动态 key 存储文件（JSON，原子写） |
| `KEY_DEFAULT_TTL_HOURS` | `24` | 新生成 key 的默认有效期（小时）；`/admin/keys` 传 `ttl_hours: 0` 可签发永久 key |
| `MASK_MODELS` | `true` | 模型掩码开关；开启后对外只暴露 `PUBLIC_MODELS` 目录，上游模型名从所有响应中抹除 |
| `PUBLIC_MODELS` | `ds41f_cus:DeepSeek V4.1 Flash:opencode/big-pickle` | 对外模型目录，格式 `对外id:展示名:上游模型`（逗号分隔多条） |
| `LOG_FORMAT` | `text` | 日志格式：`text`（人类可读）/ `json`（JSON 行，便于采集归档） |
| `REQUESTS_PER_MINUTE` | `60` | 每个网关 key 的限流 |
| `HOST` | `127.0.0.1` | 绑定地址；设 `0.0.0.0` 供局域网调用（**必须**同时设置 `GATEWAY_API_KEYS`） |
| `PORT` | `8787` | 网关端口 |
| `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` | `https://api.openai.com` / 空 | 仅 `openai` 透传模式 |

## 日志

每次 chat 记录：`request_id`、`model`、`stream`、`has_tools`、`status`、
`latency_ms`、`usage`、`client`（来源 IP）。401 鉴权失败与 429 限流记录
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
pytest balancer/tests/ -q           # LB 单测（10 项，含 failover / key 改写）
python scripts/e2e_hermes.py        # 端到端：本机 hermes 真实请求
```

端到端与 drive 细节见 `scripts/e2e_hermes.md`。
CI：`.github/workflows/ci.yml`（Python 3.12.13 + pytest + ruff；`test` 与
`balancer` 两个 job）。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| `AGENTS.md` | 维护约定、架构、免费层校验三要素（**改代码前必读**） |
| `docs/dual-bucket-topology.md` | 已验证的双桶拓扑、实测容量数据、**已知坑**（改部署前必读） |
| `docs/roadmap.md` | 正式网关 + 正式对外网站路线图与待拍板决策点 |
| `docs/engineering-constraints.md` | 工程化基线与红线 |
| `CHANGELOG.md` / `CONTRIBUTING.md` / `SECURITY.md` | 变更记录 / 协作 / 安全策略 |

## 仓库治理

- `start_gateway.bat`：Windows 一键启动（双击）——`HOST=0.0.0.0`、`PORT=8791`、
  `GATEWAY_API_KEYS=dev-local-key`，局域网设备即可调用。
- `balancer/run.cmd.example`：LB 配置模板（填好真实 key 的 `run.cmd` 不入库）。
- `AGENTS.md`：维护约定与架构说明（**改动前先读**，内含免费层校验机制的
  实测结论，勿重复踩坑）
- `CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`
- `docs/engineering-constraints.md`：工程化基线
