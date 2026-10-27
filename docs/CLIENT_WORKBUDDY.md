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
| 上下文 | 输入 **160000** / 输出 **32000**（照 `models.dev` 的 `big-pickle` 条目，见 [`MODEL_PROFILE.md`](MODEL_PROFILE.md)；曾填 131072/32768 是旧估算，会让客户端早 29K 就开始压缩） |
| 压缩点 | **136000** 提示词 token（客户端 `calculateCeiling`：160000 − 24000）；服务端在 160000 处以 400 `context_length_exceeded` 兜底 |
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

**思考强度没有开关**：`big-pickle` 的目录条目只声明 `reasoning: {supported:
true}`，没有 `effort` 列表，opencode 本体因此不提供调节项。桌面版若把
`reasoning_effort` 塞进请求，网关会丢弃该字段（`ZEN_REASONING_POLICY=max`）并
记一条 `thinking controls dropped`，模型按自身上限推理——这是对齐，不是降级。

### 2.1 兜底链（单 IP 部署的必需品，2026-10-28 实测事故）

本机实例只有一个公网 IP、没有出口池，免费层额度按 IP 每日重置。额度一旦用尽，
上游对**每个**请求都答 `429 FreeUsageLimitError`，而客户端只会把它渲染成
「请求过于频繁，请等几秒再发送」——等几秒没有任何用，要到次日 UTC 零点。

所以本机实例必须挂兜底链（`gateway_wb.bat` 已内置）：

```bat
set ZEN_FALLBACK_BASE_URL=http://115.29.231.25:10003
set ZEN_FALLBACK_API_KEY=<proxy_ms 的 key>
set ZEN_FALLBACK_DEFAULT_MODEL=ZhipuAI/GLM-5.3-Flash
set ZEN_FALLBACK_HOLD_S=300
set ZEN_FALLBACK_RECOVER_AFTER=2
```

* `ZEN_FALLBACK_BASE_URL` **不带 `/v1`**：适配器自己拼 `/v1/chat/completions`，
  写成 `.../v1` 会拼出 `/v1/v1/...` 得到 404（0.7.5 起配置层会把尾部的 `/v1`
  归一化掉，但别依赖它）。
* 触发即熔断：首个 429 / 5xx / 传输失败就切 `proxy_ms`（ModelScope
  `ZhipuAI/GLM-5.3-Flash`），熔断窗口内所有请求直接走备份，不再等主链。
* 验收：`curl -s http://127.0.0.1:8787/healthz` 应出现
  `"fallback":{"secondary":"http://115.29.231.25:10003","model":"ZhipuAI/GLM-5.3-Flash"}`；
  实测主链 429 时 `scripts/e2e_workbuddy.py` 仍 **6/6 通过**（含工具调用），
  日志会有 `FALLBACK ROUTING: model 'big-pickle' -> 'ZhipuAI/GLM-5.3-Flash'`。
* 备份链只保「能继续干活」，不保「同一个模型」：GLM-5.3-Flash 的上下文与
  工具调用能力够跑桌面端，但答案来自另一个模型，响应头会带兜底标记。

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
