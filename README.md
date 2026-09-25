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

长期维护约束见 `docs/engineering-constraints.md`：Python 固定 3.12.13、Node 用最新 LTS/Stable、全程 Git/PR/CI、语义化版本、强制测试与合规边界。

## 测试

```bash
pytest tests/ -v
```
