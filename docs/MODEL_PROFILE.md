# 模型参数：与 opencode 本体逐项对齐

网关是「替上游说话」的一方，所以它报出的数字必须就是上游的数字。本文记录每个
参数的**出处**（opencode 源码行）与网关侧的行为，改之前先读这里。

事实基线：`sst/opencode` @ `dev`，2026-10-27（`git log` 最新提交同日）。不是
从文档抄的，是从代码读的；每一条都能在原地复核。

## 1. 出处

| 事实 | 源码位置 |
|---|---|
| 模型目录（限额、能力、推理档位） | `packages/core/src/models-dev/snapshot.txt`（provider `opencode`） |
| token 估算 `round(len/4)` | `packages/core/src/util/token.ts`（`CHARS_PER_TOKEN = 4`） |
| 自动压缩阈值 | `packages/core/src/session/compaction.ts`（`calculateCeiling`） |
| 输出预算 | `packages/core/src/session/model-request.ts`（`outputLimit`） |
| 「超长」被拒 → 触发压缩 | `session/compaction.ts`：`{reason: "overflow"}` |

## 2. big-pickle 的参数（照抄，勿改）

```jsonc
{
  // models.dev 快照写的是 {context 200000, input 160000}，**实测已过期**：
  // 2026-10-29 把 prompt 一路顶到上游自己的拒绝线，它的原话是
  //   "Input token count (294840) exceeds the model's maximum context
  //    length of (262139), no tokens left for generation"
  // 实测通过点（上游回显的 prompt_tokens）：57,240 / 92,440 / 104,760 /
  // 118,840 / 132,040 / 149,640 / 162,840 / 198,040 / 203,320 / 233,240
  // 全部 200；294,840 → 400。约束是 prompt + max_tokens ≤ 262,139。
  //
  // 2026-10-29 二次实测（关 guard 的金丝雀，重复取样）补上关键一点：
  // **上游是异构后端池，越过 262,139 之后结果不确定**。
  //   268,000 目标 → 2/6 通过（真 prompt 263,703 / 271,665）
  //   300,000 目标 → 4/6 通过（真 prompt 303,265）
  //   315,000 目标 → 0/6 通过
  //   响应 id 可见三类后端：chatcmpl-… / mai-api-… / 纯 hex
  // 所以对外只宣称 262,139 这个**保证值**。宣称 303k 会让约三分之二的请求
  // 落到小窗口后端后失败（换来一个 400，还先烧了配额）。
  "limit": { "context": 262139, "input": 261000, "output": 32000 },
  "capabilities": {
    "reasoning": { "supported": true },   // 注意：没有 effort 列表、没有 toggle
    "tools": { "supported": true },
    "structured_output": true,
    "temperature": true
  },
  "compatibility": { "reasoning_field": "reasoning_content" }
}
```

推论（这就是「为什么我们的网关也屏蔽思考强度」）：

- `capabilities.reasoning` 只有 `supported: true`，**没有** `effort` 数组 →
  opencode 客户端渲染不出调节项，也不会下发改动档位的字段，模型按自身上限推理。
  对照：同目录下 `deepseek-v4.1-flash-free` / `glm-5.3-flash-free` 声明了
  `effort: ["low","high","max"]`，那些模型客户端才有调节项。
- 因此网关在 `ZEN_REASONING_POLICY=max`（默认）下**丢弃**客户端送来的
  `reasoning_effort` / `thinking` / `reasoning` / `include_reasoning` /
  `budget_tokens`：转发一个不存在的开关，等于对客户端撒谎，还可能被上游当成
  降低强度的指令。被丢弃的只是字段**名字**，值不进日志。

## 3. 压缩点（222K，按实测窗口算）

`calculateCeiling(limit)`：

```
window  = limit.input || limit.context          // big-pickle: 261000（实测）
reserve = max(floor(window*0.15), window >= 32000 ? 16000 : 0)  // 39165
ceiling = window - reserve                       // 221835
```

即按实测窗口，压缩点是 **221835**；262139 是上游真正的总窗口，261000 是我们
留出输出下限后的输入上限。models.dev 快照给的 160000/136000 已经过期——
按那个数值，客户端会在 136K 就压缩，白白浪费一半可用上下文。

网关不自己压缩对话（那是客户端的事，服务端替客户端改写历史会破坏 prompt 缓存
与工具调用配对）。网关做的是同一件事的另一半：**在输入上限处给出上游同款的拒绝**。

## 4. 上下文守卫

`ZEN_CONTEXT_GUARD=1`（默认）时，请求在**发出去之前**按同一个估算器量一遍
（含注入的 marker prompt 与内置工具 schema——它们是真 token，实测固定开销约 5,141）：

| 估算值 | 网关行为 |
|---|---|
| ≤ 输入上限 | 正常转发 |
| > 输入上限（big-pickle 261000） | 本地 400，`code=context_length_exceeded`，消息里写明三个数 |

为什么不直接透传给上游：上游同样会拒，但要等一整轮；更要紧的是免费额度——
本地拒绝**不消耗配额**，也不占用出口桶。错误形状保持 OpenAI 标准，因为
opencode 正是靠这个形状识别「context too long」并启动压缩（`reason: "overflow"`），
换成网关自定义形状会让它把同一个超长请求再发一次。

`ZEN_CONTEXT_INPUT=N` 可临时改写上限（0 = 用模型目录值），只用于实验。

## 5. 输出预算

`outputLimit`：

```
requested = min(limit.output || 32000, 256000)
room      = context - measured - ceil(estimate * 1.15)
budget    = min(requested, max(1024, room))
```

`ZEN_OUTPUT_BUDGET=mirror`（默认）：客户端没给 `max_tokens` 就补上预算，给多了
就压到窗口余量（只记一条 info，写明估算输入与控制后的值）。`clamp` = 只压不补；
`off` = 不动客户端字段。字段选择与客户端一致：存在 `max_completion_tokens` 就
用它，否则用 `max_tokens`。

## 6. 目录暴露

- `/v1/models`：保持 OpenAI 标准四字段（id/object/created/owned_by），契约冻结。
  自动发现类客户端（hermes 等）只认这个形状。
- `/v1/models_cus?format=json`：**隐藏**目录，带 `limit` / `capabilities` /
  `reasoning_efforts` / `compaction_ceiling` 与 `source`。给自己人核对参数用。
- 未知上游 id：一律不加任何限额字段——给不认识的模型编数字，是网关开始拒绝合法
  流量的起点。

## 7. 与本机客户端配置的一致性

WorkBuddy 桌面版的 `~/.workbuddy/models.json` 里 `maxInputTokens` /
`maxOutputTokens` 必须与上表一致（160000 / 32000）。曾经填成 131072 / 32768，
那是更早一版的估算值，会让客户端在 131K 就以为到顶，比本体早 29K 开始压缩。

## 8. 回归门

```bash
pytest tests/test_model_profile.py tests/test_zen_policy.py -q
```

`test_big_pickle_limits_match_the_live_upstream_not_the_stale_snapshot` 与
`test_big_pickle_declares_reasoning_without_effort_levels` 是这两条事实的钉子：
上游目录一变，它们先红。

## 9. 免费目录全量档案（models.dev @ 2026-11-14，1.19.5 二进制快照交叉验证）

2026-11-14 把档案表从 4 行补齐到 12 行（与当日免费目录一一对应），并删除已
下架的 `muse-spark-1.3-contributor-free`。目录来源：`models.dev` provider
`opencode`（`GET https://models.dev/api.json`），与本机 opencode 1.19.5 CLI
二进制内嵌快照逐值核对一致。`context` 为**保证值**（守卫按它拒绝），
「实测」列是 2026-11-14 校准探针的结果（`scripts/probe_model_context.py`，
主链验证 `usage.opencode.billing_source == "free"`）：

| id | context(保证) | output | 实测边界 | reasoning efforts | temperature | structured | attachment |
|---|---|---|---|---|---|---|---|
| big-pickle | 262139* | 32000 | 368,043 通过 / 无拒绝 | —（无档位） | ✓ | ✓ | ✗ |
| mimo-v2.6-flash-free | 200000 | 32000 | 未测 | —（无档位） | ✓ | ✗ | ✓ |
| deepseek-v4.1-flash-free | 200000 | 32000 | 280,812 通过 / 无拒绝 | low/high/max | ✓ | ✓ | ✓ |
| glm-5.3-flash-free | 200000 | 32000 | 280,511 通过 / 无拒绝 | low/high/max | ✓ | ✓ | ✓ |
| longcat-2.5-free | 1000000 | 262144 | 未测 | —（仅 toggle） | ✗ | ✗ | ✓ |
| iceman-free | 262144 | 131072 | 未测 | —（无档位） | ✗ | ✗ | ✗ |
| inkling-2-small-free | 1048576 | 131072 | 未测 | none/minimal/low/medium/high/max | ✓ | ✗ | ✓ |
| beam-free | 1048576 | 131072 | 未测 | low/medium/high/xhigh/max | ✓ | ✓ | ✗ |
| flint-40b-free | 262144 | 65536 | 未测 | low/medium/high/xhigh/max | ✓ | ✓ | ✗ |
| nemotron-3.5-super-vl-free | 1048576 | 131072 | 未测 | none/minimal/low/medium/high/xhigh | ✓ | ✗ | ✓ |
| nemotron-3.5-lightning-free | 262144 | 262144 | 未测 | —（无档位） | ✓ | ✓ | ✗ |
| minimax-m3.1-flash-free | 1000000 | 512000 | 1,000,000 通过；1,080,000 拒（厂商原话 "context window exceeds limit"） | low/medium/high/xhigh/max | ✓ | ✗ | ✓ |

\* big-pickle 是实测覆盖（2026-10-29 + 2026-11-14 双日），目录仍写 200000/160000——
上游是**异构后端池**，越过保证值之后能否通过取决于落在哪个后端（10-29：
268k→2/6 通过，300k→4/6，315k→0/6）。保证值就是守卫值；实测值只是
「池子里的大窗口后端有时给到这么多」的记录，不得对外宣称。

iceman-free 特殊：`reasoning: true` 但**没有** interleaved 字段（流式不吐
独立思考 delta，`reasoning_field=""`），且 temperature / structured_output
均为 false。

## 10. 思考强度参数与 DSH 线格式取证

- 客户端唯一有效的思考档位字段是 **`reasoning_effort`**（snake_case）。
  2026-11-14 用本机网关 `PAYLOAD_DUMP_DIR` 对 DSH 做了零成本取证
  （伪模型 id → dump 在模型解析前落盘，不打上游）：DSH 线上发的就是
  `reasoning_effort`，与 `policy.py` 的 `CONTROL_FIELDS` 一致，无需别名。
- `ZEN_REASONING_POLICY=max`（默认）下，`reasoning_effort` 仅当模型档案
  声明了 efforts 且取值在列内才透传；其余控制字段（`thinking`/`reasoning`/
  `include_reasoning`/`budget_tokens`）一律丢弃，只记字段名。`client`=
  原样透传（legacy），`off`=全丢弃。
- 实测（effort 扫描，512-token 探针，全部 200）：glm-5.3-flash-free 的
  reasoning delta 长度随档位变化（low 74 / high 39 / max 241 字符——小样本
  随机性明显，但档位字段被上游接受）；minimax 五档全 200；
  deepseek-v4.1-flash-free 三档全 200 但流式未见独立 reasoning delta
  （后端把它并进 content 或剥离，与 `interleaved.reasoning_content` 声明
  不符，是上游池行为，不是网关问题）。
- 探针工具：`scripts/probe_model_context.py --effort-sweep`（本地网关
  `ZEN_CONTEXT_GUARD=0 ZEN_REASONING_POLICY=client` 下运行；每个 200 都烧
  探测 IP 的当日额度，阶梯故意保守）。

## 11. 免费层「估算器门」：32000 prompt tokens 才是单请求真顶（2026-11-16 实测）

**这是本项目此前未记录的一道门**，比模型窗口更早生效，必须与 §9 的
「保证值」区分开看。

### 11.1 事实

上游对**每一次请求**独立校验估算体积，超限即 400（`code=context_length_exceeded`），
错误体原文同时给出两个数：

```
This model's maximum context length is 200000 tokens and at most 32000 prompt
tokens are accepted. This request is about 188497 prompt tokens.
```

- **模型最大上下文**（`200000` / `262139` / `1000000`）是窗口声明；
- **`at most 32000 prompt tokens`** 是免费层对**单请求输入**的硬顶；
- `about N` 是上游估算器的结果——**它固定按 `round(chars * 0.339)` 计**，
  与内容语言/类型**无关**：自然中文、英文 filler、纯 4-byte 符号串，实测
  `about/chars` 全部落在 0.334~0.339（3 组独立阶梯：0.3380 / 0.3342 / 0.3387）。

换算成体积：**约 94.4K chars 就是免费层的单请求硬顶**，与模型无关。

### 11.2 为什么 §9 的 368K/1,000,000 能过

§9 那次探针用英文 filler 阶梯，走到 368K tokens 仍 200——因为**真实 tokenizer
把内容折得更密**：`usage.prompt_tokens` 与 `about` 是两个不同的数，前者是真实
BPE 计数，后者是上游控制面的估算。

2026-11-16 用 4-byte 罕见字符（U+20000 / U+2A700，真实密度实测 **4.09~4.21
tok/char**，而估算器只按 0.339 计）可以把真实 tokens 藏在 94K chars 以内，
于是能在**不超估算门**的前提下把 usage 顶到：

| 模型 | 实测接受（usage.prompt_tokens） | filler |
|---|---|---|
| deepseek-v4.1-flash-free | 384,470 | U+20000 × 94000 |
| big-pickle | 384,468 | U+20000 × 94000 |
| minimax-m3.1-flash-free | 384,468 | U+20000 × 94000 |
| glm-5.3-flash-free | 384,179 | U+2A700 × 94000 |

**但这 384K 是测量技巧的产物，不是可用容量。** 常规文本（自然中文/英文）
永远先撞 94.4K chars 的估算门：实测中文 64,124 chars → 200，96,209 chars → 400。
对外给客户端配窗口时，**只能按估算门口径**，不得宣称 384K。

同轮 4-byte 字符上探到 94,300/95,000/100,000 runes 全部 400，`about`
32,044/32,218/33,468——证明天花板是**字符数**而不是 tokens。

### 11.3 对客户端/网关配置的含义

| 层 | 该填什么 | 理由 |
|---|---|---|
| 网关 context guard | 保持目录保证值 | 它是 opencode 契约的一半；放开会让客户端不压缩 |
| hermes `context_length` | **64000**（引擎硬下限） | 低于 64000 `agent_init` 直接 `ValueError`；且 hermes 用 4 chars/token 估算，0.75 阈值 = 192K chars，早已越过 94.4K 门，主压缩不会先触发 |
| 实际压缩 | 靠 `trigger_source=overflow` | 撞上游 400 后立即压缩，日志可见；这是唯一生效路径 |
| 输出预留 | 目录 `output` 值 | `_compute_threshold_tokens` 会按 `context - max_tokens` 算输入预算 |

hermes 侧可复现配置：`scripts/configure_hermes_zenedge.py`（写 provider +
model_overrides 两块，`--dry-run` 可先看）。

### 11.4 网关 guard 的已知缺口（未修，待决策）

`policy.py` 的 `ContextOverflow` 用 `estimate_request()`（即 `round(chars/4)`）
对照 `profile.limits.window()`。对照 94.4K chars 的真门，各模型放行倍数：

| 模型 | guard 阈值(window) | 折合 chars | 上游真门 chars | 放行倍数 |
|---|---|---|---|---|
| big-pickle | 261,000 | 1,044,000 | ~94,479 | 11.1× |
| deepseek / glm | 200,000 | 800,000 | ~94,479 | 8.5× |
| minimax | 1,000,000 | 4,000,000 | ~94,479 | 42.3× |

实测确认：deepseek 发 440,016 chars（中文自然文本）→ 网关**放行**，上游 400
（`about=118468`）。即 guard 对免费层这道门**完全失效**，代价是白跑一轮往返。
修它会同时收紧对**所有**客户端的放行面（含付费模型与 opencode 本体），
属于契约级改动，需单独立项评估，不在本次配置范围。

### 11.5 复现命令

```bash
# 四模型稳定性 + 延迟（每模型 5 发流式）
python3 scripts_local/stability_ctx_probe.py

# 估算门口径验证（中文自然文本，看 about/chars 恒定）
python3 scripts_local/ctx_probe.py http://115.29.231.25:10002 "$KEY" \
    deepseek-v4.1-flash-free 26000
```
