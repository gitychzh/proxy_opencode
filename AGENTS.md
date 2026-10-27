# AGENTS.md — 本仓库维护约定（改代码前必读）

> **文档地图（同一事实只在一处维护）**：
> - 本文件 = **协议事实与安全红线**（硬约束）
> - [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) = 拓扑、端口注册表、出口池语义、ONT 驱动、ADR
> - [`docs/OPERATIONS.md`](docs/OPERATIONS.md) = 当前部署状态、常用操作、排障与风险
> - [`docs/ACCESS.md`](docs/ACCESS.md) = 入口、凭据名称与去向（不含值）
> - [`docs/EGRESS_OBSERVABILITY.md`](docs/EGRESS_OBSERVABILITY.md) = 事件流与逐请求账本规格
> - [`docs/SELF_OPTIMIZATION.md`](docs/SELF_OPTIMIZATION.md) = 自我优化闭环（诊断规则、执行分级、兜底保证）
> - [`docs/engineering-constraints.md`](docs/engineering-constraints.md) = 工程化基线
> - [`README.md`](README.md) = 对外说明；[`CHANGELOG.md`](CHANGELOG.md) = 版本历史

## 项目概述

`proxy_opencode`：OpenAI 兼容网关。默认 **`zen-direct`** 直连 OpenCode Zen（镜像
opencode 1.19.4 客户端协议）；另保留 `opencode-serve`（驱动本机官方 serve）与
`openai`（通用透传）。生产部署 = ECS 单容器 + 多设备出口池，见 ARCHITECTURE。

## 硬约束（不可违反）

### 1. 免费层协议事实（多层门）

历次活体复测：2026-09-30 / 10-02 / 10-13 / 10-17 / 10-24 / 10-25。**各门独立，
顺序为：形状 → 传输栈 → session → body → 模型 → 真实配额。**

- **形状门（403 `FreeTierError`）**：至少 **4 个 distinct 工具**，`function.name`
  属于内置集（`bash/edit/glob/grep/read/skill/task/todowrite/webfetch/websearch/write`），
  且 `stream: true`。**schema/description 内容不校验**（4 真名 + 空 schema 亦 200）；
  UA 版本、头名（含 2.0 系改名）、`b3`/`traceparent`/`x-opencode-client` 值均不敏感。
  1/2/3 真名、掺改名工具、`stream:false` → 403。默认 `ZEN_TOOLS_MODE=minimal`
  注入 `bash,edit,glob,read`。
- **传输栈门（429，伪装成配额）**：上游按 TLS/HTTP 栈判定。同一新出口 IP 分钟级
  对照：bun（boringssl/HTTP2）字节级克隆 **200**，httpx（OpenSSL/HTTP1.1）同字节
  **429**（头级单变量全通过）。**生产出网必须走 bun**：镜像内置 bun +
  `zen_bun/forward.ts` 一次性子进程（`ZEN_BUN_TRANSPORT=1`，经当前桶 agent 作
  HTTPS 代理，SSE 直通）；httpx 仅 dev/test 回退。**换 IP 对 httpx 无解。**
- **session 单次使用**：`x-opencode-session` 被上游按单次使用判定——重放已消费
  会话（即便 IP 健康、headers/body 字节相同）回同款 429（同 IP：新 mint=200、
  立即重放=429，该 IP 随后仍能服务新 mint ≥3 发）。网关**每次上游试探都重新
  mint**（`zen_direct.chat()` 循环内 `_headers()`）。
- **session 编码**：`x-opencode-session` 必须 descending
  （`~((ms<<12)+counter)&0xFFFFFFFFFFFF`，同毫秒 counter < 64）；ascending 恒定
  403。`x-opencode-request`（`msg_`）仍 ascending。1.19.4 实发双头
  `x-opencode-session` + `x-opencode-session-id`（同值），**不发** legacy
  `x-session-id`，不重复 `x-opencode-request-id`；`x-opencode-project` 为 40-hex。
- **body 门（快照新鲜度）**：上游校验请求体是否"本请求的现场渲染"。同 IP、同
  bun、同样新 mint session 的 2×2 对照：官方客户端 200 / 网关 429；UA、工具集、
  AGENTS 附录单独都不是判别项，**`Today's date:` 停在抓包当天就 429，刷成当天即
  200**（同段还含模型名与工作区路径）。因此 `zen_prompt_default.txt` 只是**结构
  模板**，`_render_prompt(model)` 每请求重写 `model named X` /
  `The exact model ID is opencode/X`（用别名映射后的实际上游 ID）/
  `Today's date: <UTC 当天>` / 工作区 → `/workspace/opencode`。
  **严禁原样冻结发送或手工简化模板**（旧日期/旧模型名/`D:\proxy_opencode` 会被
  回同形 429）。`max_tokens` 无关；用户消息可任意短。`ZEN_MARKER_MODE` 默认
  `full`（bridge 注记 ~70 token 过不了门）。回归测试
  `test_prompt_asset_is_a_live_client_capture` /
  `test_render_prompt_binds_model_date_and_workspace` /
  `test_prompt_date_matches_client_date_string` /
  `test_body_marker_renders_requested_model_not_capture_model`。
- **模型门（401 `AuthError: Missing API key`）**：免费层只服务免费裸 ID
  （`-free` 后缀或 `big-pickle`）；付费裸 ID（如 `deepseek-v4.1-flash`）同
  headers/body 回 401，且 failover 链不会因 401 触发。匿名 GET
  `https://opencode.ai/inference/v1/models`（`Bearer public`，不经网关）可列免费
  目录（85 个 ID，GET 不烧额度）。别名改写用 `ZEN_MODEL_MAP`（精确命中优先于
  provider 前缀剥离）。
- **真实配额（429 `FreeUsageLimitError`）**：按**出口 IP / UTC 日**计量成功调用，
  UTC0 重置（额度小，数十发 200 量级即见烧穿；`Retry-After` 指向 UTC0）。传输
  失败、TLS EOF、超时、403 **不烧额度**，不得据此判断配额。

**429 归因红线**：`429 FreeUsageLimitError` 有**四个互斥成因且响应完全同形**——
①传输栈 ②body 快照 ③session 复用 ④真实 IP 配额。**换 IP 只对 ④ 有效**；把
①②③ 当 ④ 处理会触发无谓 ONT 轮换（实测单日 32/20/13 次）并打散隧道。
排障顺序：**传输 → prompt 新鲜度 → session → 才怀疑 IP 配额**；不得仅凭 429
触发换 IP。

**排障红线**：桶出口上**成功（200）**的活体调用会消耗该 IP 当日额度——实验少而
精、clone-first（原样先行）；429/403/401/传输失败不烧额度。

403 failover / cordon / 换 IP 的**防御性语义保留**（ARCHITECTURE §6），但根因
归因以本节的判定模型为准。模型目录自 2026-10-02 起用免费裸 ID；官方 1.19.4 实发
UA `opencode/1.19.4 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4`（值不敏感，
保持镜像）。

### 2. 回环限定

- `OPENCODE_SERVE_URL` 只允许 loopback（`config.py` 强制校验）
- `OPENCODE_ZEN_BASE_URL` 只允许 `opencode.ai` / loopback（`zen_direct` 校验）

### 3. 工程化门禁（必须全绿）

改代码必须同步改测试与文档。CI 无条件执行，任一侧红都拦：

```bash
pytest tests/
pytest balancer/tests/
ruff check proxy_opencode tests balancer
mypy proxy_opencode
cd egress-agent && pytest tests/ -q && ruff check egress_agent tests && mypy egress_agent
```

语义化版本 + CHANGELOG；commit 用 conventional commits。门禁**不得**写"未安装
则跳过"（会让它静默失效）。

### 4. 日志红线（不可违反）

绝不记录消息内容、prompt、tools 详情、`Authorization`、API Key 或任何 key。请求
观测只记路由、时延、状态码、异常分类等非敏感字段。`httpx`/`httpcore` 的 INFO
访问日志会打印带凭据的 URL，`setup_logging` 已压到 WARNING，**不得回退**
（`test_setup_logging_silences_httpx_access_lines`）。详见
[`docs/EGRESS_OBSERVABILITY.md`](docs/EGRESS_OBSERVABILITY.md)。

### 5. 凭据不入仓库

真实密钥、口令、ONT 凭据一律不提交。去向见 [`docs/ACCESS.md`](docs/ACCESS.md)。
`tests/test_no_secrets.py` 扫描全部跟踪文件作为防回归闸门。

## 开发规范

- **最小改动**：新增协议 → `formats/` 转换器 + `routes/` 薄路由；新增上游模式 →
  `upstreams/` 的 `UpstreamAdapter` 实现并在 `build_adapter` 注册。
- **保持一致**：改动需与周边结构、命名、风格一致；不造轮子（优先既有组件）。
- **文档同步**：影响拓扑/运维/入口时同步 ARCHITECTURE / OPERATIONS / ACCESS。
- **可观测**：统计各桶请求量（成功/失败，含 429/5xx/超时）从 JSONL 事件读取，
  且**必须带 `bucket_name + egress_ip`**。
