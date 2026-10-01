# AGENTS.md — 本仓库的维护约定（给所有 agent 的必读说明）

## 这个项目是什么

`proxy_opencode`：一个 OpenAI 兼容（`/v1/chat/completions`、`/v1/models`）的
本机网关，默认以 **`zen-direct` 模式直连 OpenCode Zen**（重构 opencode
1.18.32 客户端协议，使用免费模型 `opencode/big-pickle` 等）；另保留
`opencode-serve`（驱动本机官方 serve）与 `openai`（通用透传）两种模式。

## 硬约束（不可违反）

1. **免费层协议事实（2026-09-27 消融 + 2026-09-30 复测精化，勿再走弯路）**：
   服务端对匿名免费层请求做请求体指纹校验。**2026-09-30 活体复测（可复现）
   把规则精确到「名字」层面**：
   - ① **至少 2 个工具，且其 `function.name` 属于 opencode 内置工具集**
     （bash/edit/glob/grep/read/skill/task/todowrite/webfetch/websearch/write）
     ——**schema 内容完全不校验**：`bash+read` 空参数(246 字符) 200、
     仅名字(104 字符) 200、全量 schema(7,872 字符) 200；
     **只有 1 个工具 → 403**；**两个非内置名 → 403**；`bash+read+edit` → 200。
     因此默认 `ZEN_TOOLS_MODE=minimal` 只注入两个合成最小 schema（~60 token）；
     `captured2` / `all` 为回退档。
   - ② **`stream: true`**——免费层只服务流式请求，stream=false → 403。
     网关因此恒流式调 zen，非流式客户端由网关聚合 SSE。
   - ③ 系统提示词**不再被校验**：一句 tiny prompt 甚至无 system 消息均 200
     （2026-09-27 曾要求 opencode 默认提示词，已失效）。
   辅助事实：消息/会话 ID 需为 opencode 标识符格式（乱造格式 → 403）；
   0.2.0 时期"TLS 指纹校验"的结论**是错的**（mitm 两侧均可 200）；
   免费额度按出口 IP 计（429 `FreeUsageLimitError`，**但 429 是间歇限流，
   不等于日配额耗尽**——实测同桶 429 后数分钟即恢复 200）。
   资产 `zen_prompt_default.txt` / `zen_builtin_tools.json` 仅在
   `ZEN_MARKER_MODE=full` / `ZEN_TOOLS_MODE=all|captured2` 时被使用。
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
SSE 透传，死桶自动跳过。配置模板见 `balancer/run.cmd.example`，完整拓扑、实测
数据与已知坑见 **`docs/dual-bucket-topology.md`（改拓扑前必读）**。

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
