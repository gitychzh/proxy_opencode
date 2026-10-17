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

`proxy_opencode`：OpenAI 兼容（`/v1/chat/completions`、`/v1/models` 等）的本机网关。默认以 **`zen-direct` 模式直连 OpenCode Zen**（重构 opencode 1.18.32 客户端协议，使用免费模型裸 ID `big-pickle` 等）；另保留 `opencode-serve`（驱动本机官方 serve）与 `openai`（通用透传）两种模式。

详细架构、拓扑、端口、驱动等见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 硬约束（不可违反）

1. **免费层协议事实（活体复测：2026-09-30/2026-10-02/2026-10-13）**
   - 匿名请求门槛：至少 2 个工具，且 `function.name` 属于 OpenCode 内置工具集（`bash/edit/glob/grep/read/skill/task/todowrite/webfetch/websearch/write`），并且 `stream: true`。少于 2 个、使用两个非内置名、或 `stream: false` 均得到 403。默认 `ZEN_TOOLS_MODE=minimal` 注入两个合成最小 schema。
   - **session ID 必须 descending 编码（opencode 1.19.0 起）**：`x-opencode-session` 必须是 descending 编码（`~((ms<<12)+counter)&0xFFFFFFFFFFFF`），同毫秒内 `counter < 64`。ascending 恒定 403 `FreeTierError`。`x-opencode-request`（`msg_`）仍为 ascending。详见实测边界。
   - 模型目录自 2026-10-02 起使用裸 ID（如 `big-pickle`）；旧 `opencode/big-pickle` 已失效。
   - 配额错误：`FreeUsageLimitError` 表示该出口 IP 当前触发免费额度限制（按出口 IP / UTC 日独立计量、UTC 0 点重置）。传输失败、TLS EOF、超时不是 429，不得据此判断配额。
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