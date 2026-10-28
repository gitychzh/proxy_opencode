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

`test_big_pickle_limits_match_the_shipped_catalogue_snapshot` 与
`test_big_pickle_declares_reasoning_without_effort_levels` 是这两条事实的钉子：
上游目录一变，它们先红。
