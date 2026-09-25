# 端到端验证：hermes -> proxy_opencode -> opencode serve -> Zen big-pickle

## 前置

1. 本机已安装并能运行官方 `opencode`（npm 版，`opencode --version` 可查）。
2. 本机已安装 hermes（`hermes --version`）。
3. 在 hermes `config.yaml` 的 `providers:` 下注册网关（见 README）
   —— provider 名示例为 `proxyo`。

## 步骤

```bash
# 终端1：官方 serve（回环 + 密码）
set OPENCODE_SERVER_PASSWORD=你的密码
opencode serve --hostname 127.0.0.1 --port 4096

# 终端2：网关
set UPSTREAM_MODE=opencode-serve
set OPENCODE_SERVE_URL=http://127.0.0.1:4096
set OPENCODE_SERVER_PASSWORD=与终端1一致
set GATEWAY_API_KEYS=dev-local-key
python -m proxy_opencode

# 终端3：单发冒烟
hermes chat --provider proxyo -m opencode/big-pickle --oneshot --cli -q "Reply with exactly: pong"

# 批量（36 条：问答 + 推理强度 + 工具调用（--yolo 自动放行工具））
python scripts/e2e_hermes.py
# 报告写到 cap/e2e_report.json（路径见脚本顶部常量）
```

## 验收口径（2026-09 实测基线）

- 36/36 请求 rc=0；工具类提示词会真实落盘文件（`--yolo`）；
  `--reasoning high` 在 CLI 中显示 Reasoning 面板。
- 平均时延 ~8-15s/条（免费层有限流，串行为宜）。

## 常见坑

- 直连 `https://opencode.ai/zen/v1/...` 用 curl/python 会被 FreeTierError
  拒之门外（客户端指纹校验）——必须经由 `opencode serve`。见 AGENTS.md。
- `/api/session/{id}/wait` 会在空闲时返回 503，网关内部用轮询
  `GET /api/session/{id}/message`。
- hermes 的 `Messages: N (…, 0 tool calls)` 摘要统计的是最后一轮，
  不代表中间没有工具调用；以工作目录文件变化/会话日志为准。
