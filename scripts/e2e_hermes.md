# 端到端验证：Hermes → proxy_opencode → OpenCode Zen

## 前置

1. 已安装 Hermes；运行 `hermes --version` 确认 CLI 可用。
2. 网关已启动且 Hermes provider 指向该网关（README 有本地开发示例）；模型 ID 为 `ds41f_cus`。
3. 真实 E2E 会消耗上游请求与额度。优先跑 mock 协议 E2E；仅在需要验证真实网络/客户端时运行本页步骤。

## 步骤

```bash
# 启动本地开发网关。端口由项目默认配置决定，开发默认 8787；部署桶端口以 ACCESS.md 为准。
GATEWAY_API_KEYS=dev-local-key python -m proxy_opencode

# Hermes provider 已配置为 proxyo 时，执行一次真实请求
hermes chat --provider proxyo -m ds41f_cus --oneshot --cli -q "Reply with exactly: pong"

# 批量问答、推理、工具调用；工具测试会在工作目录写文件
python scripts/e2e_hermes.py
```

脚本可配置变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `E2E_HERMES_BIN` | PATH 中的 `hermes` | Hermes 可执行文件路径 |
| `E2E_PROVIDER` | `proxyo` | 已配置的 Hermes provider 名 |
| `E2E_MODEL` | `ds41f_cus` | 网关公开模型 ID |
| `E2E_WORKDIR` | 当前目录 | 工具用例工作目录 |
| `E2E_REPORT` | `./e2e_report.json` | JSON 报告路径 |
| `E2E_N` | `36` | 请求条数；真实请求会消耗上游资源 |

## 验收与诊断

- 冒烟：退出码 0，响应包含预期内容；工具用例以实际创建的文件为准，不以模型自述代替。
- `stream=false` 由网关聚合 SSE；`stream=true` 是 SSE 流式响应。
- 403：检查公开模型 ID、工具名/数量和 stream 形态，并查看上游返回；不要假定必须开启系统代理或“特定出口区域”。最近协议实测显示 marker 不是强制门槛，具体事实以 `AGENTS.md` 为准。
- 429 `FreeUsageLimitError`：按该网关出口 IP 和 UTC 日单独判断；不要把一个桶的响应推广为全局额度状态。
- DNS、连接、TLS EOF 与超时是网络故障，不是配额结论。需要诊断时分开测 healthz、models 和 chat，并对瞬时错误重试。
- 模型回答质量和数学准确性受上游模型影响；E2E 的网络/API 成功与答案正确性应分别记录。
