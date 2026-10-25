# AGENTS.md — 本仓库维护约定（改代码前必读）

> **文档地图（同一事实只在一处维护，其余引用而非复制）**：
> - 本文件 = **协议事实与安全红线**（硬约束）
> - [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) = 0.7.x 新栈权威（拓扑、端口注册表、ONT 驱动、策略路由、Egress 健康判定）
> - [`docs/OPERATIONS.md`](docs/OPERATIONS.md) = 当前部署状态、日常操作与排障
> - [`docs/ACCESS.md`](docs/ACCESS.md) = 访问入口、API Key 与凭据去向（不含凭据值）
> - [`docs/EGRESS_OBSERVABILITY.md`](docs/EGRESS_OBSERVABILITY.md) = `egress_request` JSONL 埋点与统计
> - [`docs/engineering-constraints.md`](docs/engineering-constraints.md) = 工程化基线
> - [`README.md`](README.md) = 对外说明与文档地图
> - [`CHANGELOG.md`](CHANGELOG.md) = 版本历史

## 项目概述

`proxy_opencode`：OpenAI 兼容（`/v1/chat/completions`、`/v1/models` 等）的本机网关。默认以 **`zen-direct` 模式直连 OpenCode Zen**（重构 opencode 1.19.4 客户端协议，使用免费 `-free` 后缀裸 ID，如 `deepseek-v4.1-flash-free`）；另保留 `opencode-serve`（驱动本机官方 serve）与 `openai`（通用透传）两种模式。

详细架构、拓扑、端口、驱动等见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 硬约束（不可违反）

1. **免费层协议事实（活体复测：2026-09-30/2026-10-02/2026-10-13/2026-10-17/2026-10-24 字节级克隆消融/**2026-10-25 owin10 双桶消融**）——多层门**
   - **层 1 形状门（403 `FreeTierError`）**：至少 **4** 个 distinct 工具，且 `function.name` 属于 OpenCode 内置工具集（`bash/edit/glob/grep/read/skill/task/todowrite/webfetch/websearch/write`），并且 `stream: true`。**schema/description 内容不校验**（4 真名 + 空 schema 亦 200）；UA 版本/头名（含 2.0 系改名）/`b3`/`traceparent`/`x-opencode-client` 值均**不敏感**。少于 4 个真名（1/2/3）、掺入改名工具、或 `stream: false` 均得到 403。默认 `ZEN_TOOLS_MODE=minimal` 注入 4 个合成最小 schema（pool 前四位=1.19.4 客户端实发的 `bash,edit,glob,read`）。旧的"≥2 真名"口径是 2026-09-30 时代结论，上游已收紧。
   - **session ID 必须 descending 编码（opencode 1.19.0 起）**：`x-opencode-session` 必须是 descending 编码（`~((ms<<12)+counter)&0xFFFFFFFFFFFF`），同毫秒内 `counter < 64`。ascending 恒定 403 `FreeTierError`。`x-opencode-request`（`msg_`）仍为 ascending。1.19.4 客户端实发双头 `x-opencode-session` + `x-opencode-session-id`（同值），**不再发送** legacy `x-session-id` 稳定 UUID，也不重复发送 `x-opencode-request-id`；`x-opencode-project` 为 40-hex 工作区哈希（非 `global`）。
   - **层 2 配额门（429 `FreeUsageLimitError`）**：按**出口 IP / UTC 日**独立计量成功调用，UTC 0 点重置（额度口径小，窗口内数十发 200 量级即见烧穿；`Retry-After` 精确指向 UTC0）。传输失败、TLS EOF、超时、403 均不烧额度，不得据此判断配额。**层 1 先于层 2 判定**（配额烧穿的 IP 上形状缺陷请求仍 403）。注意：层 2 之上还有**传输栈门**（见下条）——httpx 请求被伪装成层 2 429 早于任何真实计量。
   - **传输栈门（2026-10-24 PM 定案，对“配额门”单因解释的修正）**：上游按
     **TLS/HTTP 传输栈指纹**判定客户端——同一新出口 IP 分钟级对照：bun
     （boringssl/HTTP2）字节级克隆重放 **200**、httpx（OpenSSL/HTTP1.1）
     同字节重放 **429**（头级单变量全通过，唯一差异=栈）。发作形态=层 2
     同款 `FreeUsageLimitError`（伪装成配额）。**生产网关出网必须走 bun 传
     输**（镜像内置 bun + `proxy_opencode/zen_bun/forward.ts` 一次性子进程，
     `ZEN_BUN_TRANSPORT=1`，经当前桶 agent 作 HTTPS 代理出站，SSE 直通，
     池/换 IP/故障转移语义不变）；httpx 仅 dev/test 回退。换 IP 对 httpx 无解。
   - **session 单次使用（2026-10-24 PM 第四轮定案）**：`x-opencode-session` 被上游按**单次使用**判定——把已消费会话重放，即便出口 IP 健康、headers/body 字节相同，也回层 2 同款 429（同 IP 分钟级对照实测：新 mint=200、立即重放=429，且该 IP 随后仍能连续服务新 mint，≥3 发）。因此网关**每次上游试探（含 failover 链）都重新 mint**（`zen_direct.chat()` 循环内 `_headers()`）。层 2 的 429 有两个互斥成因：真实 per-IP/UTC 日配额 与 session 复用，二者都要求先过形状门+传输栈门。
   - **body 形状门（2026-10-25 第五轮定案，对“层 2 单因解释”的再修正）**：上游还校验**请求体真实性**——同出口 IP、同 bun 传输、同样新 mint session 的分钟级对照：真 1.19.4 抓包 body（38KB）**200**；网关合成 body（tiny messages + 合成工具）**429**（层 2 同款 `FreeUsageLimitError` 伪装）。逐项消融定案：**system prompt 必须是当前真身**（1.19.4 实发 prompt，12779B；旧版资产 8528B 不达标 → `zen_prompt_default.txt` 已更新）；工具 schema 内容无关（4 合成最小 schema 亦 200）；`max_tokens` 无关（50 亦过）；用户消息可任意短。**`ZEN_MARKER_MODE` 默认已改 `full`**（bridge 注记 ~70 token 过不了门）。注意 body 门先于层 2 配额判定，其 429 与配额 429 同形，无法从响应区分。
   - **模型门（401，2026-10-25 发现）**：免费层只服务 **`-free` 后缀裸 ID**；付费裸 ID（如 `deepseek-v4.1-flash`）同 headers/body 回 **401 `AuthError: Missing API key`**（非 403/429，failover 链不会触发）。匿名 GET `https://opencode.ai/inference/v1/models`（`Bearer public`，不经网关）可列免费目录（85 个 ID，GET 不烧额度）。公网别名 → 免费裸 ID 的改写用 `ZEN_MODEL_MAP`（精确命中优先于 provider 前缀剥离）。
   - **排障红线（2026-10-24 教训，随第三轮修正）**：桶出口上**成功（200）**的活体调用会消耗该 IP 当日匿名额度——活体实验少而精、原样先行（clone-first）；429/403/401/传输失败不烧额度。当日"整窗 count=0 却全池 429"的最终归因是**传输栈门**（httpx 全部被伪装 429），非探针烧穿（早前推断已撤，见上条）。
   - 403 failover/cordon/换 IP 的**防御性语义保留**（见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §5），但根因归因以**形状门 + 传输栈 + session 单次使用 + body 门 + 模型门**判定模型为准（见上）。
   - **日志红线补充（2026-10-25）**：`httpx`/`httpcore` 的 INFO 访问日志会打印完整 URL（池探针 URL 的 userinfo 携带 agent Basic 凭据）——`setup_logging` 已强制压到 WARNING，不得回退（回归测试 `test_setup_logging_silences_httpx_access_lines`）。
   - 模型目录自 2026-10-02 起使用裸 ID（如 `big-pickle`）；旧 `opencode/big-pickle` 已失效。官方 1.19.4 实发 UA：`opencode/1.19.4 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4`（UA 值不敏感但保持镜像）。
   - 诊断与更多细节见 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)。

2. **回环限定**
   - `OPENCODE_SERVE_URL` 只允许 loopback（`config.py` 强制校验）
   - `OPENCODE_ZEN_BASE_URL` 只允许 `opencode.ai` / loopback（`zen_direct` 校验）

3. **工程化门禁（必须全绿）**
   改代码必须同步改测试与文档。门禁（CI 无条件执行，两侧任一红都拦）：
   ```bash
   pytest tests/
   pytest balancer/tests/
   ruff check proxy_opencode tests balancer
   mypy proxy_opencode
   # 0.7.x egress-agent 侧
   cd egress-agent && pytest tests/ -q && ruff check egress_agent tests && mypy egress_agent && cd ..
   ```
   语义化版本 + [`CHANGELOG.md`](CHANGELOG.md)；commit 信息用 conventional commits。

4. **日志红线（不可违反）**
   绝不记录消息内容、prompt、tools 详情、`Authorization`、API Key 或任何 key。请求观测仅记录路由、时延、状态码、异常分类等非敏感字段。详见 [`docs/EGRESS_OBSERVABILITY.md`](docs/EGRESS_OBSERVABILITY.md)。

5. **凭据不入仓库**
   真实密钥、口令、ONT 凭据一律不提交至仓库。存放去向见 [`docs/ACCESS.md`](docs/ACCESS.md)。

## 开发规范

- **最小改动**：新增协议 → `formats/` 加请求/响应转换器 + `routes/` 加薄路由。新增上游模式 → `upstreams/` 加 `UpstreamAdapter` 实现并在 `build_adapter` 注册。
- **保持一致**：改动需与周边代码结构、命名、风格一致。
- **文档同步**：改动影响拓扑/运维/入口时，同步对应文档（优先 `ARCHITECTURE.md`/`OPERATIONS.md`/`ACCESS.md`）。
- **可观测**：统计各桶请求量（成功/失败，含 429/5xx/超时等所有失败）时，应从 `egress_request` JSONL 读取，且**必须带 `bucket_name + egress_ip`**。