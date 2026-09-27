# AGENTS.md — 本仓库的维护约定（给所有 agent 的必读说明）

## 这个项目是什么

`proxy_opencode`：一个 OpenAI 兼容（`/v1/chat/completions`、`/v1/models`）的
本机网关，默认以 **`zen-direct` 模式直连 OpenCode Zen**（重构 opencode
1.18.32 客户端协议，使用免费模型 `opencode/big-pickle` 等）；另保留
`opencode-serve`（驱动本机官方 serve）与 `openai`（通用透传）两种模式。

## 硬约束（不可违反）

1. **免费层协议事实（2026-09-27 实测，勿再走弯路）**：
   - 校验机制 = **请求体标记（opencode 真实系统提示词）+ 出口 IP 区域 +
     客户端指纹** 三元组。只有协议头（UA/x-opencode-*）没有 body 标记 →
     必 403 `FreeTierError`（与 TLS 指纹无关，mitm 两侧均可复现）。
   - 0.2.0 时期"TLS 指纹校验"的结论**是错的**：当时所有直连测试都没带
     body 标记。真机 mitm 抓包（TLS 被拦截替换）照样 200。
   - `httpx trust_env=True` 在 Windows 会经 urllib 读**注册表系统代理**
     （Clash 7897 等）；`trust_env=False` 强制 CN 直连出口 → 必 403。
   - 免费额度按出口 IP 计（429 `FreeUsageLimitError`），共享 VPN 出口易触顶。
   - 因此 `zen_direct.py` 的 `trust_env=True` 与标记注入**不可移除**；
     资产 `zen_prompt_default.txt` 必须与 opencode 发布版系统提示词同步。
2. **回环限定**：`OPENCODE_SERVE_URL` 只允许 loopback（config.py 强制校验）；
   `OPENCODE_ZEN_BASE_URL` 只允许 opencode.ai / loopback（zen_direct 校验）。
3. **工程化**：改代码必须同步改测试与文档；`pytest tests/` 全绿才可提交；
   语义化版本 + CHANGELOG；commit 信息用 conventional commits。
4. **日志红线**：绝不记录消息内容、prompt 或任何 key。

## 架构

```
hermes / 任意 OpenAI SDK
  -> proxy_opencode.app          # FastAPI 装配（只接线）
    -> routes/                   # 薄 HTTP 层（chat.py, models.py）
    -> security.py               # Bearer 鉴权 + 限流
    -> upstreams/                # 适配器协议 + 实现
       - zen_direct.py           # 直连 Zen（协议重构 + 标记注入 + SSE 透传）
       - zen_prompt_default.txt  # opencode 1.18.32 系统提示词资产（标记）
       - opencode_serve.py       # 驱动官方 serve API（回退模式）
       - tools_contract.py       # serve 模式的 JSON 工具调用契约桥接
       - openai_http.py          # 通用 OpenAI 透传
       - fields.py               # 请求字段白名单
```

新增上游模式 = 在 `upstreams/` 加一个实现 `UpstreamAdapter` 协议的模块 +
`build_adapter` 里注册一行。

### zen-direct 的关键事实（实测确认，opencode 1.18.32 / 2026-09-27）

- 端点：`POST {base}/chat/completions`，Zen 就是 OpenAI 兼容格式；流式
  SSE 逐字节透传，`reasoning_content` 与原生 `tool_calls` 直接可用
  （不再需要 serve 模式的 JSON 契约桥接与合成流）。
- 协议头（抓包复刻）：`Authorization: Bearer public`（匿名）、
  `User-Agent: opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`、
  `x-opencode-client: cli`、`x-opencode-project: global`、
  `x-opencode-session: ses_<26>`、`x-opencode-request: msg_<26>`。
- 标识符算法（`schema/src/identifier.ts`）：26 字符 = `(ms<<12)+counter`
  的低 48 位 hex（12 字符）+ 14 字符随机（byte%62 映射 [0-9A-Za-z]）；
  已用本机 opencode.db 真机「ID/时间戳」配对逐对验证。
- 匿名免费层：无密钥时 models 目录中 cost>0 的模型不可用，免费模型
  （如 big-pickle）`allowAnonymous`。
- 响应尾部有非标准 chunk `data: {"choices":[],"cost":"0"}`，客户端需容错。

### serve 桥接的关键事实（回退模式，opencode 1.18.x）

- serve API v2：`POST /api/session`（带 model + 内建工具 permission deny 规则；
  **不能用 v1 prompt 的 `tools` 禁用映射**——实测会让 Zen 免费层恒定
  FreeTierError 403；permission deny 只拦执行、保留 schema，安全）→ `POST /api/session/{id}/prompt`
  (`{"prompt": {"text": ...}, "delivery": "steer"}`) → 轮询
  `GET /api/session/{id}/message` 直到出现 finish 非空的 assistant 消息 →
  `DELETE /session/{id}` 清理。**`POST /api/session/{id}/wait` 不可靠**
  （空闲也返 503），不要用。
- assistant 消息：`content[]` 中 `type=text|reasoning|tool`；`tokens` 上有
  input/output/reasoning/cache。reasoning 映射为 OpenAI `reasoning_content`；
  内部 tool part 仅放 metadata（`tools_source=opencode-agent`）。
- 外部 function-calling 工具无法注册进 serve，用 JSON 契约桥接（详见
  `tools_contract.py` docstring）。stream 为合成单 chunk
  （`metadata.synthetic_stream=true`），serve API 无 token 流。
- Zen 免费层限流明显；短时高并发会 429。网关串行化 serve 回合
  （`_turn_lock`）。

## 本地开发

```bash
uv venv --python 3.12.13 .venv
uv pip install -e '.[test]' --python .venv/Scripts/python.exe
pytest tests/ -q
```

端到端（需要本机已装 hermes）：见 `scripts/e2e_hermes.md`。

## 版本与环境基线

- Python 固定 3.12.13（pyproject requires-python 锁定）。
- Node 工具链未引入；若将来引入，提交 lockfile。
- 上游参考实现源码：本机 `D:\wb_ps\opencode-src`（GitHub anomalyco/opencode
  浅克隆，含 v1.18.32 tag；仅用于阅读，不要提交进本仓库）。
- 抓包材料：`cap/`（`.gitignore` 排除）——mitm 流量日志、回显服务器、
  A/B 定位脚本；换新 opencode 版本时需重抓并同步 `zen_prompt_default.txt`。
