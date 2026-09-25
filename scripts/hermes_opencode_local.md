# 本机 hermes 调试：opencode-cli 上游模式（本机 / 官方 CLI / 受限）

> **仅限本机回环调试。** 该模式让网关把 OpenAI-compatible 请求转交给
> **本机已安装的官方 `opencode` CLI** 执行。网关不实现、也不允许任何
> 风控绕过、免费额度放大、客户端指纹伪造或对外分发凭证；也不会绕过
> opencode 官方客户端的版本检查。单日额度（如 800 次/天）与账号限制
> **完全由官方 CLI 控制**，网关不叠加任何 rate bypass。

## 何时使用

- 本机调试 hermes 与网关的 OpenAI API 链路，而不想走 `UPSTREAM_BASE_URL`
  的 HTTP 上游。
- 默认模式仍是 `UPSTREAM_MODE=openai`（HTTP 上游），不受影响。

## 前置条件

- 本机已安装并登录官方 `opencode` CLI（`opencode` 在 PATH 中，或用
  `OPENCODE_BIN` 指向完整路径）。
- 账号额度、免费池可用模型由官方 CLI 决定；网关只按
  `OPENCODE_ALLOWED_MODEL_PREFIXES`（默认 `opencode/`）做**收窄**过滤，
  决不放行免费池以外的模型，除非你显式改配置。

## 启动（只监听本机回环）

```bash
export UPSTREAM_MODE=opencode-cli
export GATEWAY_API_KEYS=dev-local-key        # 网关侧 key，不要外发
# 可选：
# export OPENCODE_BIN=opencode
# export OPENCODE_MODELS_CMD="opencode models"
# export OPENCODE_RUN_TIMEOUT_S=120
# export OPENCODE_ALLOWED_MODEL_PREFIXES="opencode/"
# export MODELS_CACHE_TTL_S=300
# export OPENCODE_XDG_DATA_HOME=/path/to/xdg-data   # 传给子进程的 XDG_DATA_HOME

uvicorn proxy_opencode.app:app --host 127.0.0.1 --port 8787
```

hermes 侧：

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=dev-local-key          # 与 GATEWAY_API_KEYS 一致
```

## 行为与限制

- `/v1/models`：调用 `opencode models`，按 allow prefix（默认 `opencode/`）
  过滤后返回 OpenAI models list；结果按 `MODELS_CACHE_TTL_S`（默认 300s）
  缓存。CLI 不可用/超时/非零退出 → 502（OpenAI 风格错误）。
- `/v1/chat/completions`：
  - 非 stream：把 system/user/assistant 的文本消息拼成单个 prompt，以
    参数列表形式（**不用 shell**）调用 `opencode run -m <model> <prompt>`；
    stdout 原样作为 content 返回。stderr/非零退出 → 502。响应
    `metadata.adapter = "opencode-cli"`。
  - stream：不伪装真流式，返回**单个 SSE chunk + `[DONE]`**，并在
    `metadata.synthetic_stream = true` 标明。
  - `tools` / `tool_calls` / `response_format` / `reasoning_effort`
    非空时直接 **400**（该适配器不支持工具/思考透传，不会静默丢弃）。
  - 模型必须命中 `OPENCODE_ALLOWED_MODEL_PREFIXES`，否则 400。
- 日志只记 model/cmd/exit_code/latency/status，**不记录消息内容与任何 key**。
- 子进程固定 timeout（`OPENCODE_RUN_TIMEOUT_S`，默认 120s）；禁 `shell=True`。

## 明确不做的事

- 不做风控/限流绕过、不做免费额度放大、不伪造客户端身份、不向本机以外
  分发任何凭证、不绕过 opencode 官方版本检查。
- 不要把这个模式绑定到 `0.0.0.0` 或暴露到局域网/公网。
