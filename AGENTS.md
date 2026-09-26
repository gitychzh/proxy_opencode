# AGENTS.md — 本仓库的维护约定（给所有 agent 的必读说明）

## 这个项目是什么

`proxy_opencode`：一个 OpenAI 兼容（`/v1/chat/completions`、`/v1/models`）的
本机网关，把 hermes 等客户端的请求转给**本机官方 `opencode serve`**（回环
HTTP API），从而合规地使用 OpenCode Zen 的免费推理模型（`opencode/big-pickle`
等）。另保留一个通用 `openai` 透传模式。

## 硬约束（不可违反）

1. **合规边界**：绝不直连官方 Zen 端点伪装 opencode 客户端指纹。实测非
   opencode 客户端（curl/httpx/python-requests）调用
   `https://opencode.ai/zen/v1/chat/completions`（哪怕 UA/头完全复刻）会被
   `FreeTierError: free tier can only be used from within OpenCode` 拒绝
   ——服务端做 TLS/HTTP2 指纹校验。正确路径是经由本机官方 `opencode serve`
   代理，它是真正的 opencode 客户端。免费额度与风控全部归官方服务端管。
2. **回环限定**：`OPENCODE_SERVE_URL` 只允许 loopback（config.py 强制校验）。
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
       - opencode_serve.py       # 驱动官方 serve API（无状态桥接）
       - tools_contract.py       # JSON 工具调用契约桥接
       - openai_http.py          # 通用 OpenAI 透传
       - fields.py               # 请求字段白名单
```

新增上游模式 = 在 `upstreams/` 加一个实现 `UpstreamAdapter` 协议的模块 +
`build_adapter` 里注册一行。

### serve 桥接的关键事实（实测确认，opencode 1.18.x）

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

端到端（需要本机已装 opencode + hermes）：见 `scripts/e2e_hermes.md`。

## 版本与环境基线

- Python 固定 3.12.13（pyproject requires-python 锁定）。
- Node 工具链未引入；若将来引入，提交 lockfile。
- 上游参考实现源码：本机 `..\opencode-src`（GitHub anomalyco/opencode 浅克隆，
  仅用于阅读，不要提交进本仓库）。
