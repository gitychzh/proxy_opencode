# hermes 本机联调：opencode-serve 上游模式（回环、官方服务端）

让网关走本机官方 `opencode serve` 的 HTTP API（`/api` 路由）。合规边界：
免费额度、速率限制与所有风控策略**完全由官方 opencode 服务端控制**，本网关
只按 Basic auth 调用本机回环接口，不抓包、不绕过、不共享或放大免费额度。

## 1. 启动官方 opencode serve（127.0.0.1 回环）

```bash
# 必须显式设置密码；不设置则官方端无鉴权（只有 unsecured warning），
# 且网关也只允许连 127.0.0.1。务必绑定回环地址。
export OPENCODE_SERVER_PASSWORD='选择一个强密码'
opencode serve --hostname 127.0.0.1 --port 4096
```

## 2. 启动网关（UPSTREAM_MODE=opencode-serve）

```bash
export UPSTREAM_MODE=opencode-serve
export OPENCODE_SERVE_URL=http://127.0.0.1:4096   # 只允许 loopback
export OPENCODE_SERVER_USERNAME=opencode
export OPENCODE_SERVER_PASSWORD="$OPENCODE_SERVER_PASSWORD"  # 与上一致
export GATEWAY_API_KEYS=dev-local-key             # 否则是 dev-open 模式
uvicorn proxy_opencode.app:app --host 127.0.0.1 --port 8787
```

可选：非流式慢时加大 `OPENCODE_SERVE_WAIT_TIMEOUT_S`（默认 300s）。

## 3. 验证

```bash
curl -s -H "Authorization: Bearer dev-local-key" http://127.0.0.1:8787/v1/models

curl -s -H "Authorization: Bearer dev-local-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/big-pickle","messages":[{"role":"user","content":"用一句话回答 1+1=?"}]}' \
  http://127.0.0.1:8787/v1/chat/completions
```

注意：`content` 里该模型的免费额度与限制以官方服务端实际返回为准；连不上返回
502，超时返回 504；`tools`/`tool_choice`/`response_format`/`reasoning_effort`
非空会被网关显式拒绝（400），不会静默吞掉。
