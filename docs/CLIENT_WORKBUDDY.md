# 客户端接入：WorkBuddy 桌面版

> 目标：桌面版把本机网关当作一个 OpenAI 兼容的自定义模型使用。
> 网关侧协议见 [`AGENTS.md`](../AGENTS.md)，运维见 [`OPERATIONS.md`](OPERATIONS.md)。

## 1. 桌面版如何读自定义模型（已实测确证）

WorkBuddy 桌面版由 `CustomModelsProductProvider` 从**文件**加载自定义模型，路径：

```
C:\Users\<user>\.workbuddy\models.json
```

* 顶层是**数组**（也接受 `{models: [...], availableModels: [...]}`）；
* 文件被 `fs.watch` 监视，写入后约 1 秒内自动重载，无需重启应用；
* 每项字段（现网文件已确证）：`id`、`name`、`vendor`、`url`、`apiKey`、
  `supportsToolCall`、`supportsImages`、`supportsReasoning`、
  `useCustomProtocol`、`maxInputTokens`、`maxOutputTokens`；
* `apiKey` / `url` 支持环境变量引用；`CODEBUDDY_DISABLE_CUSTOM_MODELS_FILE=1`
  会整体关闭该文件加载；`WORKBUDDY_CONFIG_DIR` 可改目录（默认 `~/.workbuddy`）。

加载是否成功，看主进程日志：

```powershell
Select-String -Path "$HOME\.workbuddy\logs\main.log" -Pattern "CustomModelsProductProvider|Loaded custom models" | Select-Object -Last 5
```

出现 `Loaded custom models config from user: ...` 即生效；若出现
`Skipping model without id` 或 `Failed to load user...` 则是配置格式问题。

## 2. 本机网关实例（127.0.0.1:8787）

| 项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | `wb-local-8787`（仅本机回环，`HOST=127.0.0.1`） |
| 模型 ID | `deepseek-ai/deepseek-v4.1-flash`（对外唯一别名，上游 `big-pickle`） |
| 上下文 | 输入 131072 / 输出 32768（桌面版字段上限，网关侧仍受上游约束） |
| 协议 | OpenAI 兼容（`useCustomProtocol: false`），另支持 `/v1/responses`、`/v1/messages` |

启动方式：Windows 计划任务 `proxy_opencode-wb-gateway`（SYSTEM，登录即启动，
失败自动重启 3 次），执行
`C:\ProgramData\proxy_opencode\gateway_wb.bat`，日志
`C:\ProgramData\proxy_opencode\gateway.log`。

**为什么必须走 bun**：匿名免费层按 TLS/HTTP 栈判定请求（AGENTS.md 硬约束 1）。
同一份字节，httpx（OpenSSL/HTTP1.1）稳定拿 `429 FreeUsageLimitError`，bun
（boringssl/HTTP2）拿 200。所以本机实例固定 `ZEN_BUN_TRANSPORT=1` +
`ZEN_BUN_BIN`（bun 1.4.4，与生产镜像同版本）。缺 bun 时网关静默回退 httpx，
表现为「每条请求都 429」——那不是配额问题。

## 3. 验收（端到端）

```bash
python scripts/e2e_workbuddy.py --base http://127.0.0.1:8787 --key wb-local-8787
```

六项检查覆盖桌面版真实形态：`models`（别名不泄漏上游 id）、`chat_stream`
（SSE + 工具 + reasoning，逐块校验 model 掩码）、`chat_nonstream`（上游只允许流式，
此项验证聚合路径）、`tool_call`（返回 `tool_calls`）、`responses`（codex 事件序列）、
`messages`（Anthropic 事件序列）。全部 PASS 才算可用；退出码 0/1。

2026-10-28 实测：6/6 PASS（ttfb ~2.2–3.3s，工具调用返回
`get_time {"city": "Shanghai"}`）。

## 4. 容量与选型

本机实例**没有出口 IP 池**：每次成功调用消耗本机公网 IP 的当日免费额度
（数十发量级即烧穿，UTC 00:00 重置）。适合日常问答/短任务；长时间高强度使用
会被 `429 FreeUsageLimitError` 挡住。

需要持续吞吐时改用 ECS 网关（`https://115.29.231.25:10002/v1`，三桶出口池 +
`FailoverAdapter` → proxy_ms 兜底）。切换只需把 `models.json` 里该项的 `url` /
`apiKey` 换成 ECS 入口与其网关密钥，其余字段不变。

## 5. 回滚

```powershell
Copy-Item "$HOME\.workbuddy\models.json.bak-<时间戳>" "$HOME\.workbuddy\models.json" -Force
Unregister-ScheduledTask -TaskName 'proxy_opencode-wb-gateway' -Confirm:$false   # 需管理员
```

`models.json` 修改前已自动备份为同目录 `models.json.bak-<YYYYMMDDHHMMSS>`。
