# AGENTS.md — 本仓库的维护约定（给所有 agent 的必读说明）

## 这个项目是什么

`proxy_opencode`：一个 OpenAI 兼容（`/v1/chat/completions`、`/v1/models`）的
本机网关，默认以 **`zen-direct` 模式直连 OpenCode Zen**（重构 opencode
1.18.32 客户端协议，使用免费模型裸 ID `big-pickle` 等）；另保留
`opencode-serve`（驱动本机官方 serve）与 `openai`（通用透传）两种模式。

## 硬约束（不可违反）

1. **免费层协议事实（最近活体复测：2026-09-30；上游模型目录复核：2026-10-02）**：
   - 匿名请求实测门槛：至少 2 个工具，且其 `function.name` 属于 OpenCode 内置工具集
     （bash/edit/glob/grep/read/skill/task/todowrite/webfetch/websearch/write），并且
     `stream: true`；少于 2 个或使用两个非内置名、或 `stream: false` 均得到 403。
     当前默认 `ZEN_TOOLS_MODE=minimal` 注入两个合成最小 schema（~60 token）；
     `captured2` / `all` 是较大 schema 回退档。
   - 同次复测中，tiny prompt 或没有 system 消息也可通过；因此 system marker 不是
     已验证的门槛。默认 `ZEN_MARKER_MODE=bridge` 仍注入兼容说明，勿把“代码默认注入”
     误写成“上游强制要求”。历史实验若与这组复测冲突，以此处标注的最近实测为准。
   - 消息/会话 ID 需符合 opencode 标识符格式（乱造格式 → 403）；
     0.2.0 时期“TLS 指纹校验”的结论已证伪（mitm 两侧均可 200）。
   - 配额错误须区分：上游 `FreeUsageLimitError` 表示该出口 IP 当前触发免费额度限制，
     按出口 IP / UTC 日独立计量、UTC 0 点重置；它不代表所有桶都耗尽。传输失败、
     TLS EOF 或超时不是 429，不得据此判断配额；诊断需带重试并核对同一桶与 UTC 日期。
   Zen 模型目录自 2026-10-02 起使用裸 ID（如 `big-pickle`）；旧 `opencode/big-pickle`
   已失效。协议细节与诊断流程见 `docs/OPERATIONS.md`。
2. **回环限定**：`OPENCODE_SERVE_URL` 只允许 loopback（config.py 强制校验）；
   `OPENCODE_ZEN_BASE_URL` 只允许 opencode.ai / loopback（zen_direct 校验）。
3. **工程化**：改代码必须同步改测试与文档；`pytest tests/` +
   `pytest balancer/tests/` + `ruff check proxy_opencode tests balancer` +
   `mypy proxy_opencode` 全绿才可提交（CI 无条件执行这四项，别再写
   "未安装则跳过" 之类会让门禁静默失效的守卫）；语义化版本 + CHANGELOG；
   commit 信息用 conventional commits。
4. **日志红线**：绝不记录消息内容、prompt 或任何 key。

## 架构

```
hermes / codex CLI / claude code / 任意 OpenAI SDK
  -> proxy_opencode.app          # FastAPI 装配（只接线；工厂式，无导入期副作用）
    -> routes/                   # 薄 HTTP 层（chat / models / responses_api /
                                 #   anthropic_api / admin）
       - _pipeline.py            # 三条协议路由的共享管线：解析 → 模型别名 →
                                 #   白名单 → 调适配器 → 异常映射 → 日志。
                                 #   新增协议只写“请求转换 + 响应整形”，别再
                                 #   复制中段（复制过就会走样，见 CHANGELOG 0.6.2）
    -> formats/                  # 协议转换（responses_proto / anthropic_proto /
                                 #   sse_iter）——进转 OpenAI chat，出转回各协议
    -> auth/                     # core.py（Bearer/x-api-key 鉴权 + 管理员）
                                 #   keystore.py（动态 key 存储，默认 24h 有效期）
    -> registry.py               # 对外模型目录（掩码：ds41f_cus -> 上游模型）
    -> sse_mask.py               # 流式/非流式响应的 model 字段改写
    -> upstreams/                # 适配器协议 + 实现
       - zen_direct.py           # 直连 Zen（协议重构 + 标记注入 + SSE 透传）
       - zen_prompt_default.txt  # opencode 1.18.32 系统提示词资产（标记）
       - opencode_serve.py       # 驱动官方 serve API（回退模式）
       - tools_contract.py       # serve 模式的 JSON 工具调用契约桥接
       - openai_http.py          # 通用 OpenAI 透传
       - fields.py               # 请求字段白名单
```

多个网关实例（不同出口 IP = 不同免费配额桶）之间的轮询由 **`balancer/lb.py`**
承担：纯 ASGI least-connection 负载均衡，为每个上游改写各自的 `Authorization`，
SSE 透传，死桶自动跳过。配置模板见 `balancer/run.cmd.example`；当前节点、运行状态与
拓扑排障见 **`docs/OPERATIONS.md`（改部署前必读）**，地址与访问方式见 `docs/ACCESS.md`。

新增上游模式 = 在 `upstreams/` 加一个实现 `UpstreamAdapter` 协议的模块 +
`build_adapter` 里注册一行。新增客户端协议 = 在 `formats/` 加一对
请求/响应转换器 + `routes/` 加一个薄路由（参考 responses_api.py）。

### 双桶相关的硬约束（2026-09-28 实测）

1. **配额按出口 IP 计**：同一台机器上跑两个网关毫无意义——出口 IP 相同，
   额度就是同一份。要翻倍必须让请求从两个不同出口 IP 出去。
2. **客户端工具名不得与 opencode 内置工具同名**（`bash`/`read`/`edit`/`glob`/
   `grep`/`write`/`list`/`task`/`todowrite`/`webfetch`/`websearch`/`skill`）。
   `zen_direct.py` 的 `_merge_tools` 在同名时以客户端工具覆盖内置工具，内置
   列表被挤掉 → 免费层**静默 403**：本地无任何报错，表现只是"模型不调工具"。
   → 用 `fs_read`/`fs_exec` 之类不冲突的名字。此坑极难从症状反推，务必记住。
3. **双桶不提升单请求生成速度**（实测 chars/s -4%）：免费层限流作用于**单个
   请求**，一个长流式请求从头到尾只用一个桶。提升的是并发吞吐与抗排队
   （20 短请求墙钟 -20%）。不要拿单请求速度当验收指标。
4. **LB 的客户端 key 不得泄漏到任何桶**（有单测覆盖）；`ZEN_LB_API_KEY` 未设置
   时 LB fail-closed 拒绝一切请求。
5. **配置不得内置真实凭据**：`ZEN_LB_UPSTREAMS` 默认值必须为空（→ 503），
   真实 key 只进环境变量 / 启动脚本，`balancer/run.cmd` 已 gitignore。

### zen-direct 的关键事实（实测确认，opencode 1.18.32 / 2026-09-27）

- 端点：`POST {base}/chat/completions`，Zen 就是 OpenAI 兼容格式；网关恒
  `stream: true`（免费层要求），流式客户端 SSE 逐字节透传，非流式客户端
  由网关聚合（`_aggregate_sse`：content/reasoning_content/tool_calls 增量
  合并 + usage 捕获 + 容忍非标准 `{"choices":[],"cost":"0"}` 尾 chunk）。
- 协议头（抓包复刻）：`Authorization: Bearer public`（匿名）、
  `User-Agent: opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`、
  `x-opencode-client: cli`、`x-opencode-project: global`、
  `x-opencode-session: ses_<26>`、`x-opencode-request: msg_<26>`。
- 标识符算法（`schema/src/identifier.ts`）：26 字符 = `(ms<<12)+counter`
  的低 48 位 hex（12 字符）+ 14 字符随机（byte%62 映射 [0-9A-Za-z]）；
  已用本机 opencode.db 真机「ID/时间戳」配对逐对验证。
- 匿名免费层：无密钥时 models 目录中 cost>0 的模型不可用，免费模型
  （如 big-pickle）`allowAnonymous`。免费额度按出口 IP、按 UTC 日限流。
- **429 判读纪律（2026-10-02 深挖纠偏）**：`FreeUsageLimitError` 是真实的
  出口 IP 当日额度信号（本机直连出口在 0.6.2 消融测试期耗尽，UTC 0 点重置
  后同探测恢复 200；连发 6 次、直连/代理双路径均 200，无突发限流）。注意
  两点：① 各出口桶独立计量，别把本机 IP 的 429 说成"全局没额度"；
  ② **直连 opencode.ai 有间歇性 SSL EOF（连接被重置）**，探测必须带重试，
  否则会把网络抖动误读成服务端行为。诊断工具：`scripts/diag_zen_429.py`。
- **模型 ID 已去前缀（2026-10-02 实测）**：Zen 目录（`GET /zen/v1/models`）
  现返回裸 ID（`big-pickle`、`space-bunny-free` 等）；旧拼写
  `opencode/big-pickle` 已失效（401 `ModelError`）。zen-direct 的默认映射
  随之改为裸 ID（`DEFAULT_ZEN_MODELS` / `DEFAULT_PUBLIC_MODELS`）；
  `opencode-serve` 模式仍用 opencode 内部 `provider/model` 命名空间，勿混。
  以后怀疑漂移时先 `GET https://opencode.ai/zen/v1/models` 对账。
- 桥接说明（`_BRIDGE_NOTE`）要求模型只用客户端工具——实测模型正确调用
  客户端自定义工具而不碰内置工具（hermes e2e：write-file/ls/read 全过）。

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
uv pip install -e '.[test,dev]' --python .venv/Scripts/python.exe
pytest tests/ -q
pytest balancer/tests/ -q          # LB 单测（需 httpx/uvicorn/pytest-asyncio）
ruff check proxy_opencode tests balancer
mypy proxy_opencode
```

提交前两条 pytest 都要绿，ruff 与 mypy 也要干净（CI 会跑同样的四条）。

端到端（需要本机已装 hermes）：见 `scripts/e2e_hermes.md`。

## 版本与环境基线

- Python 固定 3.12.13（pyproject requires-python 锁定）。
- Node 工具链未引入；若将来引入，提交 lockfile。
- 上游参考实现源码：GitHub anomalyco/opencode（tag v1.18.32）。本机参考克隆
  `D:\wb_ps\opencode-src` 已于 2026-09-27 随 opencode app 一并清理；需要重看
  源码或重抓包时重新浅克隆即可。
- 本机 opencode app 已卸载（npm 全局包 + 用户数据，回收站保留）。默认
  `zen-direct` 模式是纯 HTTP 直连 opencode.ai，**不依赖本机 opencode**；
  仅 `opencode-serve` 回退模式需要本机安装 opencode CLI，当前机器上该回退
  模式不可用。
- 抓包材料：`cap/`（`.gitignore` 排除）——mitm 流量日志、回显服务器、
  A/B 定位脚本；换新 opencode 版本时需重抓并同步 `zen_prompt_default.txt`。
