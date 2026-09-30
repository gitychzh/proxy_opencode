# 端到端验证：hermes -> proxy_opencode (zen-direct) -> Zen big-pickle

## 前置

1. 本机已安装 hermes（`hermes --version`）。
2. 系统代理（如 Clash）处于开启状态——免费层校验依赖出口区域，
   CN 直连出口会被 403 `FreeTierError` 拒绝（详见 AGENTS.md）。
3. 在 hermes `config.yaml` 的 `providers:` 下注册网关（见 README）
   —— provider 名示例为 `proxyo`，指向 `http://127.0.0.1:8791/v1`。

## 步骤

```bash
# 终端1：网关（zen-direct 为默认模式）
set GATEWAY_API_KEYS=dev-local-key
python -m proxy_opencode                       # 127.0.0.1:8787

# 终端2：单发冒烟
hermes chat --provider proxyo -m ds41f_cus --oneshot --cli -q "Reply with exactly: pong"

# 批量（问答 + 推理强度 + 工具调用，--yolo 自动放行工具）
python scripts/e2e_hermes.py
# 报告默认写到 ./e2e_report.json（可用 E2E_REPORT 覆盖）
```

可覆盖的环境变量（跨桶/跨机器可移植，无需改脚本）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `E2E_HERMES_BIN` | `PATH` 中的 `hermes` | hermes 可执行文件路径 |
| `E2E_PROVIDER` | `proxyo` | hermes provider 名 |
| `E2E_MODEL` | `ds41f_cus` | 发给 hermes 的模型 id（网关对外模型） |
| `E2E_WORKDIR` | 当前目录 | 工具类用例的工作目录 |
| `E2E_REPORT` | `./e2e_report.json` | 报告输出路径 |
| `E2E_N` | `36` | 执行条数 |

## 验收口径

- 冒烟 rc=0 且回复含 `pong`；工具类提示词真实落盘文件（`--yolo`）；
  `--reasoning high` 在 CLI 中显示 Reasoning 面板。
- 平均时延 ~5-30s/条（免费层按出口 IP 限流，串行为宜；
  429 `FreeUsageLimitError` 属共享出口配额触顶，等待重试即可）。

## 常见坑

- 免费层校验 = 请求体标记（opencode 系统提示词）+ 出口 IP 区域 +
  客户端指纹。网关已自动处理标记注入；若见 403 `FreeTierError`，
  先检查出口路由（`ZEN_PROXY` / 系统代理）是否绕过了 CN 直连。
- `stream=false` 由网关聚合；`stream=true` 为真 SSE（token 级）。
- hermes 的 `Messages: N (…, 0 tool calls)` 摘要统计的是最后一轮，
  不代表中间没有工具调用；以工作目录文件变化/会话日志为准。
