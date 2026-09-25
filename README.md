# proxy_opencode

一个合规的 OpenAI-compatible 反向代理网关：把客户端的 OpenAI API 请求转发到
配置的上游（`UPSTREAM_BASE_URL`），同时提供网关侧鉴权、限流与请求日志。

**合规声明**：本网关只做协议转发与访问控制，不实现也不支持风控绕过、
免费额度滥用、客户端指纹伪造。客户端的 API key 不会透传给上游。

## 安装

```bash
cd proxy_opencode
pip install -e ".[test]"
```

## 配置（全部通过环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://api.openai.com` | 上游 OpenAI-compatible 服务地址 |
| `UPSTREAM_API_KEY` | 空 | 发给上游的 Bearer key |
| `GATEWAY_API_KEYS` | 空 | 逗号分隔的网关 key；为空时为开发模式（不鉴权） |
| `PORT` | `8787` | 监听端口 |
| `REASONING_PASSTHROUGH` | `true` | 是否透传 `reasoning_effort` / `thinking` / `include_reasoning` / `reasoning` |
| `REQUESTS_PER_MINUTE` | `60` | 每个网关 key 每分钟请求上限（内存实现） |
| `UPSTREAM_MODE` | `openai` | 上游模式：`openai`（HTTP 转发，默认）或 `opencode-cli`（本机官方 CLI，受限） |
| `OPENCODE_BIN` | `opencode` | opencode CLI 可执行文件（仅 `opencode-cli` 模式） |
| `OPENCODE_MODELS_CMD` | `opencode models` | models 命令（可配置） |
| `OPENCODE_RUN_TIMEOUT_S` | `120` | `opencode run` 子进程超时（秒） |
| `OPENCODE_XDG_DATA_HOME` | 空 | 可选；设置后传给子进程的 `XDG_DATA_HOME` |
| `OPENCODE_ALLOWED_MODEL_PREFIXES` | `opencode/` | 逗号分隔的模型前缀白名单，拒绝免费池外模型 |
| `MODELS_CACHE_TTL_S` | `300` | opencode-cli 模式下 `opencode models` 结果缓存秒数 |
| `OPENCODE_SERVE_URL` | `http://127.0.0.1:4096` | 本机 `opencode serve` 地址（仅 `opencode-serve` 模式）；**只允许 loopback**（127.0.0.1/localhost/::1），非 loopback 启动直接报错 |
| `OPENCODE_SERVER_USERNAME` | `opencode` | `opencode serve` Basic auth 用户名 |
| `OPENCODE_SERVER_PASSWORD` | 空 | `opencode serve` Basic auth 密码。**为空只允许 127.0.0.1**，且务必阅读下方强警告 |
| `OPENCODE_SERVE_MODELS` | `opencode/big-pickle` | `opencode-serve` 模式 `/v1/models` 静态列表兜底（逗号分隔） |
| `OPENCODE_SERVE_TIMEOUT_S` | `120` | opencode-serve 普通请求超时（秒） |
| `OPENCODE_SERVE_WAIT_TIMEOUT_S` | `300` | opencode-serve `session/{id}/wait` 超时（秒） |

### opencode-cli 上游模式（本机 / 官方 CLI / 受限）

仅当 `UPSTREAM_MODE=opencode-cli` 时启用，供本机 hermes 调试使用：
请求转交给**本机官方 `opencode` CLI**（参数列表 subprocess、带超时、禁
`shell=True`），models 从 `opencode models` 读取并按
`OPENCODE_ALLOWED_MODEL_PREFIXES` 过滤；chat 把文本消息拼成单个 prompt 调
`opencode run -m <model> <prompt>`；stream 返回合成单 chunk（
`metadata.synthetic_stream=true`）；`tools`/`tool_calls`/`response_format`/
`reasoning_effort` 非空直接 400。CLI 失败返回 502。响应以
`metadata.adapter=opencode-cli` 标注。**不实现**风控绕过、额度放大、
客户端伪造、对外分发凭证或版本检查绕过；额度与限制由官方客户端控制。
详见 [`scripts/hermes_opencode_local.md`](scripts/hermes_opencode_local.md)。

### opencode-serve 上游模式（本机 / 官方 HTTP API / 受限）

仅当 `UPSTREAM_MODE=opencode-serve` 时启用：请求发往**本机回环上的官方
`opencode serve`**（`/api` 路由，Basic auth 用户名默认 `opencode`、密码来自
`OPENCODE_SERVER_PASSWORD`）。chat 流程：创建 session → prompt → wait →
读取最新 assistant 消息；assistant 的 `content[]` 中 `text` 合成 OpenAI
`content`，`reasoning` 合入 `message.reasoning_content`，`tool` 只读映射为
`tool_calls` 并在 metadata 标注 `tools_source=opencode-agent`；stream 为合成
单 chunk（`metadata.synthetic_stream=true`）。客户端传
`tools`/`tool_choice`/`response_format`/`reasoning_effort` 非空直接 400。
`/v1/models` 返回 `OPENCODE_SERVE_MODELS` 静态列表并标注
`metadata.adapter=opencode-serve`。

**强警告**：`OPENCODE_SERVER_PASSWORD` 为空时官方端无鉴权（仅有 unsecured
warning），本网关此时只允许连 `127.0.0.1`；任何情况下都不允许指向非
loopback 地址。连接不上返回 502、超时返回 504。**不实现**风控绕过、额度
放大或指纹伪造；免费额度与限制完全由官方服务端控制。详见
[`scripts/hermes_opencode_serve.md`](scripts/hermes_opencode_serve.md)。

## 运行

```bash
uvicorn proxy_opencode.app:app --host 0.0.0.0 --port 8787
```

## 端点

- `GET /healthz` — 健康检查（无需鉴权）
- `GET /v1/models` — 转发到上游
- `POST /v1/chat/completions` — 转发到上游，支持 stream 与非 stream

客户端用 `Authorization: Bearer <GATEWAY_API_KEYS 中的 key>` 访问网关；
网关向上游只发送 `UPSTREAM_API_KEY`。

`/v1/chat/completions` 只按白名单透传字段：`messages`、`tools`、
`tool_choice`、`response_format`、`temperature`、`top_p`、`max_tokens` 等；
当 `REASONING_PASSTHROUGH=true` 时额外透传 reasoning 相关字段。
上游 SSE 流原样透传；上游错误以其原始状态码与 OpenAI 风格 JSON 返回。

## 日志

每次请求记录：`request_id`、`model`、`stream`、`has_tools`、`status`、
`latency_ms`、存在的 `usage`。绝不记录消息内容或任何 API key。

## 工程化约束

长期维护约束见 `docs/engineering-constraints.md`：Python 固定 3.12.13、全程 Git/PR/CI、语义化版本、强制测试与合规边界。

**Node.js**：当前项目暂未引入任何 Node 工具链，CI 也不构建前端。根目录的 `.nvmrc`（`lts/*`）仅为将来引入前端/脚本工具链时锁定最新 LTS/Stable 预留；届时需同步提交 lockfile。

## 仓库治理

- 贡献流程与 PR 模板：[`CONTRIBUTING.md`](CONTRIBUTING.md)、[`.github/pull_request_template.md`](.github/pull_request_template.md)
- 提 issue：[Bug Report](.github/ISSUE_TEMPLATE/bug_report.yml) / [Feature Request](.github/ISSUE_TEMPLATE/feature_request.yml)（GitHub 新建 issue 时自动加载）
- 安全策略与漏洞报告：[`SECURITY.md`](SECURITY.md)
- 变更记录：[`CHANGELOG.md`](CHANGELOG.md)
- 代码归属：`.github/CODEOWNERS`；依赖周更：`.github/dependabot.yml`
- 发布：推送 `v*` tag 触发 [`release.yml`](.github/workflows/release.yml)，跑测试后创建 GitHub Release

## 测试

```bash
pytest tests/ -v
```

## 本地系统化冒烟

无需真实上游 key，即可在本机回环上验证整条链路（fake upstream → 网关）：

```bash
# 一键脚本（Git Bash 可跑）：起 fake upstream + 网关，curl 验证
# healthz / models / 非流式 / SSE 流式 / tools+reasoning 透传 / 401 / 429，
# 结束后自动清理进程。全部使用 dummy key。
bash scripts/smoke_local.sh

# 等价的 pytest 集成测试（真实 uvicorn + 随机端口）：
pytest tests/test_gateway_integration.py -v
```

fake upstream 实现见 `tests/fake_upstream.py`，也可单独作为模块被其他
测试复用（`create_app()`），或独立运行：
`python tests/fake_upstream.py --port 9901`。
