# Changelog

语义化版本；格式遵循 [Keep a Changelog](https://keepachangelog.com/)。
协议事实的权威表述在 [`AGENTS.md`](AGENTS.md) 硬约束 1，本文只记变更。

## [0.8.4] - 2026-10-29

### Fixed — 单账号故障被兄弟桶吞掉，没走备用链

0.8.1 把 fallback 改成了请求级，但 `zen-direct` 的内部重试仍在原地：一个桶传输
失败 / 5xx / 403 时，循环会 `_acquire()` 换一个**兄弟桶**重试。生产演练
（`nsenter` 注入 `REJECT` 到 cc9 的 `100.87.219.115:10001` 一跳，10 请求）表现为 10/10 全 200、
零 `x-gateway-fallback` 头——请求根本没到 proxy_ms，只是换了另一个账号。这与
「任意一个账号发生故障 → 该请求 fallback 到 proxy_ms → 下一请求回轮询」不符。

现在：只要配置了 `ZEN_FALLBACK_BASE_URL`，单个桶的传输失败 / 5xx / free-tier
403 直接结束本次尝试（释放租约并把该桶 cordon），由外层 `FailoverAdapter` 把
本次请求交给备用链。未配置备用链的部署保持原来的跨桶重试，行为不变。

### Changed — 请求级 fallback 的日志不再报「hold N 秒」

`request_scoped` 模式下 `fallback_engaged` 已写 `hold_s=0`，但 `FALLBACK
RE-ENGAGED` 那行仍打印 `backup continues for 120s`，读日志的人会以为整链被按住。
现在该行按实际语义分支：请求级报「下一次请求会重试池子」，只有全池额度黑屏
（`quota_blackout`）才报 hold 秒数。

## [0.8.3] - 2026-10-29

### Fixed — 一次软探针失败就摘桶，把健康桶从轮询里踢掉

`/healthz` 答 200 但 `ready=false` / `egress_probe_ok=false` 时，池子在**第一次**
就 `healthy=False`。而 agent 侧 `ready` 的语义是「最近一次对 echo_url 的探测
成功」（8s 超时、30s 一次），所以一台 LTE 设备丢一个包就足以让整桶下线 30s。

2026-10-29 夜间复盘（5 小时窗口）：账本 26 条 `probe_failed`，全部
`agent not ready`，全部在下一个探针自愈，期间这些桶 `failures=0`
（20/21 条请求全成功）。后果不是报错，而是**流量被挤到更少的 IP 上**：
单桶承担的 prompt tokens 更多 → 日额度更早烧穿 → 硬 429。

现在：

- `ZEN_EGRESS_PROBE_FAIL_TOLERANCE`（默认 `1` = 历史行为，生产设 `2`）：
  连续 N 次软失败才摘桶；未达阈值只记 INFO，不发 `probe_failed` 事件；
- `last_probe_ok` 仍报原始结果（快照不许说坏探测成功了）；
- **传输失败 / 非 200 不受影响**，仍然第一次就摘桶（2026-10-25 僵尸桶事故：
  agent 的 tailnet 路径没了却继续接活）。

### Fixed — 开机即坏的桶在账本里完全不可见

`probe_failed` 只在「健康 → 不健康」跃迁时写。而 `EgressState.healthy` 默认
`False`，所以一个**从进程启动就坏**的桶永远不产生跃迁，也永远不写事件。

2026-10-29 实测：`owin10-telecom` 的 `bind_source=192.168.2.235` 已不在宿主机上
（`ipconfig` 只剩 Tailscale / 192.168.1.222 / 192.168.56.1，`netsh` 四个接口全
Enabled、无禁用项），绑定源地址失败 → 探测恒失败。它连续数小时不可用，账本却
显示一个干净的池子，只有 `/admin/egresses` 能看出来。

现在 `probe_alert_sent` 保证**每个故障段的第一条一定可见**（与上一状态无关），
同一段内仍只记一条（不每 30s 刷），恢复时清标志、下个故障段重新记。

### Changed — 探针失败原因可归因

`probe_failed` 的 `reason` 从统一的 `agent not ready` 拆成
`ready=false` / `egress_probe_ok=false` / `status <code>`，并附 `(n/N)` 连击数。
两者处置不同（设备上行 vs agent 状态），之前同形导致巡检报告无法归因。

### Changed — 巡检规则按「主导原因 + 是否影响请求」分流

`egress_probe_flapping` 之前对所有形态都输出同一句「设备侧上行，不要调大探针
容忍度」。三种形态的处置互斥，现在按账本里的主导 `reason` 与「是否全部自愈 /
是否有请求失败」分流：

- `egress_probe_ok=false` → 这条腿出不去网（设备/运营商线路）：换 IP、调容忍度
  都无效，先查宽带；
- 异常类名（`ConnectTimeout` 等）→ agent 本身不可达：查 tailnet 与 agent 进程；
- 全部自愈且 `ok_ratio ≥ 0.99` → 采样噪声，降级为 `提示` 并给出
  `ZEN_EGRESS_PROBE_FAIL_TOLERANCE=2` 的做法；
- 其它（含旧账本的 `agent not ready`）→ 保留原文案。

`probe_failed_reasons` 同时进入报告的 evidence，抖动不再是「一个数字」。

## [0.8.1] - 2026-10-29


### Changed — 四个出口桶按「四个账号」语义重做故障处理

之前一个桶被上游判额度耗尽，会把整条 fallback 链按住到重置窗口（实测
`hold_s=15211`，4.2 小时），期间所有用户都在备用模型上跑，即使另外三个桶
完全健康。现在：

- 单桶故障 → **只影响本次请求**（由备用链回答），`hold_s=0`；
- 下一个请求 → 回到出口池的轮询，自然落到下一个健康桶；
- 只有**全池**都被判额度时，才盖上 blackout 截止时间（此时确实无处可转，
  与其每 2 分钟重试一次死池，不如把备用链按住到重置）。

`ZEN_FALLBACK_REQUEST_SCOPED`（默认 `true`）保留旧的「一次故障锁整链」行为，
供对照实验使用。请求级语义下 `fallback_engaged` 事件的 `hold_s` 为 `0`、
`request_scoped` 为 `true`，日志口径可与旧行为区分。

### Fixed — 工具改名是单向的，响应侧从不还原

形状门要求 `tools` 里必须含全部内置工具名，所以客户端自带的同名工具会被改名
成 `client_<name>`（`bash` → `client_bash`）。请求侧改了，响应侧却没有反向
映射：模型回 `client_bash` 时，客户端收到一个自己从未声明过的工具名 ——
要么丢弃，要么当陌生工具执行。现在改名同时登记反向表（请求级 contextvar），
流式（chat SSE、responses SSE）与非流式（chat JSON、upstream error body）
四条路径都按表还原。

### Changed — 上下文上限按实测的**保证值**收敛，并记录异构后端事实

实测（2026-10-29，两次独立扫描）：

- `models.dev` / 快照说的 `{context 200000, input 160000}` **偏小**：258,906
  prompt tokens 正常返回 200；
- 所有拒绝文案统一引用 `262139`，且绑定规则是 `prompt + max_tokens ≤ 262139`
  （实测 261,792 + 1,024 = 262,816 → 400，超出 677）；
- 越过 262,139 后结果**不确定**，因为上游是异构后端池。同一 payload 重复：
  268k 档 2/6 通过、300k 档 4/6 通过（真 prompt 303,265）、315k 档 0/6；
  响应 id 可见三类后端（`chatcmpl-…`、`mai-api-…`、纯 hex）。

因此 guard 保留 262,139 这个唯一**保证**窗口（不是乐观的 303k）：
宣称 303k 会让三分之二的请求打到小窗口后端后失败。本地拦截不烧配额。

### Changed — 客户端指纹追到 1.19.5，新增上游漂移自动检测

- `DEFAULT_ZEN_CLIENT_VERSION` 从 `1.19.4` 改为 `1.19.5`（真实 CLI 抓包确认
  UA `opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4`）。
  资产早已是 1.19.5，常量却停在 1.19.4 —— 正是这类漂移没有检测手段。
- 新增 `scripts/sync_opencode.py`：比对 npm `opencode-ai` 的 `latest`、官方
  prompt 模板（`session/prompt/default.txt`，纯文本无插值，与本地资产可直接
  字节比对）、以及 `tool/builtins.ts` 的模块清单。**能自动化的只有版本号和
  prompt 模板**；工具 schema 是 TS 的 Effect `Schema`，没有可靠的 JSON Schema
  静态转换，必须用真实 CLI 抓包重取 —— 脚本对此明确报「需要重新抓包」而不是
  假装修好。
- 新增 `.github/workflows/drift.yml`：每日 09:17 CST 跑一次，漂移即 fail。

> 抓包另证：1.19.5 的 `builtins.ts` 装了 12 个模块，但实际只发 **11 个**工具名
> （`question` 与 `apply-patch` 注册但不提供给模型）。我方资产保留 12 个属于
> 超集，形状门接受超集、拒绝子集，故无需改动。

## [0.8.0] - 2026-10-29

### Changed — 入口与网关拆成两个容器（框架重构）

网关这一侧现在必须跟着上游的形状门频繁改动（内置工具集、prompt 模板、日期），
而 `10002` 过去既是网关又是唯一入口 —— 重建一次就把用户踢下线一次。0.8.0 把两
件事分开：

```
客户端 ──10002──> zen-gw-edge ──primary──> zen-gw:10002 ──> Zen 上游
                        └───────fallback──> 172.17.0.1:10003 (proxy_ms,
                                            deepseek-ai/DeepSeek-V4.1-Flash-1017)
```

- 新增 `proxy_opencode/edge/`：**纯 ASGI 透传**（不算业务、不校验请求体），只在
  响应首字节发给客户端**之前**做一次选路，所以切换不会截断 SSE。
  - 降级判据：探针失败（2s 周期）、连接被拒/超时、`502/503/504`、以及
    `Retry-After > 1h` 或 `code=free_quota_exhausted` 的 429。
  - `400/401/404/422` 等 4xx **不**降级：那是答案不是故障，掩盖它只会让排障更难。
  - 备用请求改写 model（`EDGE_FALLBACK_MODEL`）与 `Authorization`
    （`EDGE_FALLBACK_API_KEY`），并把响应里的 model 改回客户端请求的 id；客户端
    能看到的差异只有响应头 `X-Edge-Route` / `X-Edge-Fallback-Reason`。
  - 只有 `/v1/chat/completions` 可降级：备用中继只说这一种协议，另外两种协议
    宁可保留原错误，也不静默换成另一种格式。
  - `/edge/status`、`/edge/mode`（需 `EDGE_ADMIN_KEY`）支持重建前手动 drain。
- `compose.yml`：`zen-gw` 不再发布端口（`expose 10002`，只在 compose 网络内），
  由 `edge` 发布宿主 `10002`；两个服务共用同一镜像（`zen-gw:0.8`，command 不同），
  一次构建、两处运行，不存在版本漂移。镜像别名随版本从 `zen-gw:0.7` 改为
  `zen-gw:0.8`。
- 生产 `.env` 的 `ZEN_FALLBACK_DEFAULT_MODEL` 由 GLM 改为
  `deepseek-ai/DeepSeek-V4.1-Flash-1017`，进程内备用链路与 edge 的备用链路指向
  同一个模型，避免"主链挂掉后到底给了哪个模型"两套答案。

### 验收

- `GET /healthz` 同时返回 `role: edge`、`primary.alive`、`fallback.configured`。
- 停掉 `zen-gw` 后同一请求仍 200，响应头 `X-Edge-Route: fallback`，模型 id 仍是
  客户端请求的那个。
- 16 个新测试覆盖透传、4xx 不降级、连接失败降级、配额 429 降级、非 chat 路径
  不降级、admin 开关。

## [0.7.17] - 2026-10-29

### Fixed — 真因：body 门，不是额度

**更正 0.7.12–0.7.16 的结论**：那几版把 `429 FreeUsageLimitError` 当成账号级额度
耗尽来治，是错的。用真实 CLI（opencode 1.19.5）在本机抓包重放做对照后，同 IP、
同 bun 传输栈下：

| 请求体 | 结果 |
|---|---|
| CLI 原始请求 | 200 ×3 |
| 我方旧 body（1.19.4 的 10k prompt + 4 个内置工具） | 429 ×3 |
| 我方 body 但只保留 1 个内置工具 / 1 个自造工具 | 429 |
| 我方 body + 12 个内置工具 | 200 |
| 12 个内置工具 + 1 个自定义工具 | 200 |
| CLI 模板 + 桥接说明 + **UTC 日期（Oct 28）** | 429 ×3 |
| 同一份 + **CST 日期（Oct 29）** | 200 ×3 |

两个真因：

- **资产过期**：`zen_builtin_tools.json` 是 1.19.4 时代被裁到 4 个名字的子集，
  而 1.19.5 的门要求**当前客户端的完整 12 个内置工具**（子集即 429）；
  `zen_prompt_default.txt` 同理过期（9960 → 9558 字符，且抓取时混入了抓包机
  自己的 `Instructions from: ...CLAUDE.md`，已清掉）。
  修法：`ZEN_TOOLS_MODE` 默认 `full`（12 个内置全注入 + 客户端工具追加在后），
  客户端工具若与内置同名则重命名为 `client_<name>` 并同步改写历史
  `tool_calls`（内置名字一个都不能少）。
- **日期用了 UTC**：桥接说明在场时，UTC 日期会被判「不是本请求的实时渲染」而
  返回同形 429。改为按上游日界（+8）渲染，`ZEN_PROMPT_UTC_OFFSET_H` 可覆盖。

### Fixed — 公开别名没有映射到上游 id

`_zen_model()` 只查 `ZEN_MODEL_MAP`，生产没配它，于是公开别名
`deepseek-ai/deepseek-v4.1-flash` 剥前缀后变成**付费** id
`deepseek-v4.1-flash` → `401 AuthError`（被 fallback 链掩盖成「主链不可用」）。
现在第二顺位查 `PUBLIC_MODELS` 的第三段（别名 → 上游 id），别名发布即生效。

### 保留

0.7.12–0.7.16 的 `quota_blackout` / `ZEN_ROTATE_ON_HARD_429=false` 仍然保留：
真正的额度耗尽时它们是对的，只是这次不是额度。

## [0.7.16] - 2026-10-29

### Changed

- **硬配额 429 第一个桶就定论，不再扫兄弟桶** — 既然额度是账号级，收到
  `Retry-After > 3600s` 的 429 后另外两个桶注定同样结果：实测首请求 11.8s
  （3 次上游试探）→ 现在 1 次试探即停，省下 2 个 RTT。软 429（短
  Retry-After）行为不变，仍逐个桶 failover。证据：
  `test_final_429_retry_after_becomes_the_blackout_deadline`（断言
  `call_count == 1`）。

## [0.7.15] - 2026-10-29

### Fixed

- **首个请求仍拿不到重置期限（0.7.14 的漏网）** — `FAILOVER_MAX_ATTEMPTS=3`
  而池里有 4 个桶，所以「每个桶都被 bench」这个条件在额度刚耗尽时**永远不
  成立**，`quota_blackout_s()` 恒为 0，最后仍退化成固定 120s 熔断（实测首个
  请求 10.9s，0.7.14 上复现）。改为直接采信响应证据：`_release_for_status`
  把 `Retry-After > HARD_QUOTA_RETRY_AFTER_S(3600s)` 的 429 盖上
  `__quota_blackout_s__`，尝试预算耗尽时用它作为熔断期限。
  证据：`test_final_429_retry_after_becomes_the_blackout_deadline`。

## [0.7.14] - 2026-10-29

### Fixed

- **额度刚耗尽的那一个请求仍然走完四个桶** — 0.7.12 的黑名单只在请求**开始**
  时判定，重启后的进程 cordon 状态是空的，于是第一个请求照样逐个桶试（实测
  7.14s 才拿到 fallback 的 16 token 回答）。429 分支现在每次桶被判配额耗尽
  后立即复查 `quota_blackout_s()`，全池见底就停手并带上重置期限，首个请求
  也走「秒回 + 熔断撑到重置」的路径。证据：
  `test_chat_stops_retrying_once_a_429_tips_the_pool_into_blackout`。
  （该判断在 4 桶 3 次尝试下不成立，0.7.15 用 Retry-After 证据补齐。）

## [0.7.13] - 2026-10-29

### Fixed

- **额度耗尽会把四个桶全部送进换 IP，重启 ONT 却换不来任何额度** — 0.7.12
  部署后第一次 429，四个桶依次触发 `hard_429` 轮换：`owin10-mobile` /
  `owin10-telecom` / `cc9` 重启光猫，`esp32-bucket` 重启 MiFi。既然额度是
  账号级，这些重启一个字节的额度都拿不到，反而每条链路断网 ~65s、隧道重建、
  家庭网络抖动。新增 `ZEN_ROTATE_ON_HARD_429`（**默认 false**）：硬 429 只
  cordon 到窗口重置，不再动硬件。证据：
  `test_hard_429_does_not_rotate_by_default`。
- **「配额耗尽但远低于 token 预算 = 预算配错」这条 ERROR 会天天误报** — 额度
  是账号级，单桶 token 计数根本预测不了它（兄弟桶烧的是同一份额度，账本被清
  空时更是 0）。改为 WARNING、只在 `0 < tokens < budget` 时输出，文案写明
  「account-scoped，此预算无法预测」。

## [0.7.12] - 2026-10-29

> 0.7.9–0.7.11 的条目未补写（当时的变更只在 `git log` 里，事后重建等于伪造
> 记录，故留空）。

### Fixed

- **全池额度耗尽时每个请求白跑 4 次上游** — 免费层额度是**账号级**（实测
  2026-10-29 02:00 CST）：四个出口 IP、ECS 自己的 IP，以及刚重启 MiFi 换来的
  全新 IP（`111.55.72.119` → `111.55.74.1`）在几分钟内先后返回同一
  `FreeUsageLimitError`，`Retry-After` 全部指回 08:00 窗口重置。换句话说
  **换 IP 只换限流桶，换不来额度**；池里所有桶带着配额判定被 bench 之后，
  再逐个桶试一遍只是把 fallback 链推后一个上游 RTT/桶（实测 16 token 的回答
  耗 7.25s）。新增 `EgressPool.quota_blackout_s()`：仅当**每个**桶都带
  `free_quota_exhausted` / `free_quota_soft` 判定时返回剩余秒数，适配器据此
  直接返回 429 + `__quota_blackout_s__`，一次上游都不打。
  证据：`test_quota_blackout_requires_every_bucket_to_be_benched`、
  `test_chat_answers_quota_blackout_without_touching_upstream`。
- **配额型 429 只按住 fallback 熔断 120s，两分钟后又去撞死墙** — 熔断窗口是
  固定 `ZEN_FALLBACK_HOLD_S`，而账号额度要到窗口重置才恢复：每 2 分钟就有一个
  请求先走完死池再被备用模型回答。现在 `_trip` 接受 `blackout_s`，按
  `min(blackout, 24h)` 撑满熔断窗口并在账本里记 `quota_blackout=true`。
  证据：`test_quota_blackout_holds_breaker_until_reset`、
  `test_quota_blackout_hold_is_capped_at_one_day`。

### Changed

- 配额熔断原因文案 `per-IP daily quota` → `account daily quota`：口径错了，
  会误导 operator 去做无效的 IP 轮换（本次实测就是被它带偏的）。
- 日志白名单新增 `blackout_s`（`logsetup.FIELDS` 是 `extra` 进日志的唯一通道）。

## [0.7.8] - 2026-10-28

### Fixed

- **诊断在「账本被窗口裁光」这一支上直接崩掉** — `diagnose.py` 第 264 行起用的是
  `findings.append(_finding(...))["evidence"].update(...)`，而 `list.append` 返回
  `None`，`["evidence"]` 于是抛 `TypeError: 'NoneType' object is not subscriptable`。
  触发条件恰好是最需要报告的形态：整本账本落在窗口外。本轮实测复现——
  `.venv\Scripts\python.exe scripts/self_optimize.py ... --events-window-hours 5`
  在 53 条账本、7 条 `fallback_failed` 的情况下崩溃退出，报告一个字都没写出来。
  修法：先构造 finding、更新 `evidence`、再 append。证据：
  `test_all_events_dropped_emits_a_clock_skew_finding_instead_of_crashing`。
- **5 小时窗口在本机实例上永远读不到账本：日志本地时间 vs 账本 UTC** — 本机
  `LOG_FORMAT=text` 的日志行头是**本地时间**（`2026-10-28 12:17:22`，UTC+8），
  事件账本写 UTC（`2026-10-28T04:17:22+00:00`）。`record_epoch()` 把无时区时间
  按 UTC 解释，锚点因此超前 8 小时，`--events-window-hours 5` 把整本账本裁光，
  报告只留一句误导性的「事件账本可能未开启」。实测：锚点
  `2026-10-28T12:09:01+00:00`、丢弃 52 条、`fallback_failed` 全部消失；加
  `--log-tz local` 后锚点回到 `2026-10-28T04:16:55+00:00`、保留 53 条，
  7 条 `fallback_failed` 正常触发 critical。修法：`window_events()` /
  `summarize()` / `analyze()` 增加 `log_tz_offset_s`，CLI 增加
  `--log-tz {utc,local}`（`local` 从 `astimezone().utcoffset()` 取偏移）。
  容器日志是 UTC，默认值保持 `utc`。证据：
  `test_window_events_aligns_a_local_time_log_with_the_utc_ledger`。

### Changed

- **换 IP 的主触发从「请求条数」改为「token 额度」** — 上游免费层按 token 计量，
  不是按调用次数。2026-10-28 实测：本机 08:00–15:52 只发了 **166 条**请求就吃
  429（`Retry-After` 指向次日 UTC0），而同窗口累计 prompt 是 **10,347,914
  tokens**（WorkBuddy 长上下文，均值 62k/条）；ECS 三桶全天合计仅 52 条 /
  1.87M tokens，`owin10-mobile` 的 IP 仍一天死两次（04:14 换到 223.68.47.105 →
  07:57 又耗尽）。原来的 `ZEN_EGRESS_ROTATION=500` 条阈值因此**永不触发**（单桶
  日计数 7–15 条），两次轮换全靠 hard 429 reactive，也就是先吃墙再换 IP。
  改动：新增 `ZEN_EGRESS_TOKEN_ROTATION`（每桶每窗口估算 prompt tokens 上限，
  默认建议 8M 对 ~10M 额度留 20% 余量）作为主闸，条数阈值保留为粗粒度副闸；
  `BucketCounts.tokens` 随快照持久化、随 08:00 窗口与成功轮换归零。
  计量口径由 `ZenDirectAdapter` 在取租约时把上下文闸已算出的
  `Applied.estimated_input` 戳到 `Lease.tokens`，`EgressPool.release` **仅在
  ok 路径**折叠（429/5xx 不消耗额度）。证据：
  `test_token_budget_fires_before_the_request_threshold`、
  `test_token_only_bucket_still_rotates`、
  `test_chat_stamps_the_prompt_estimate_on_every_lease`。
  注意：LTE 桶是 CGNAT 共享出口，邻居流量烧的是同一个 IP 的额度，自有 token
  计量无法覆盖这部分——那一类仍只能靠 hard 429 reactive 兜底。

## [0.7.7] - 2026-10-28

### Fixed

- **非流式请求没有卡顿看门狗，一个静默桶吃掉整个 90s 主链预算** — 生产实测
  两条 `stream:false` 请求 `latency_ms=96475.4` / `97941.8`，都带
  `fallback=proxy_ms`、`fallback_reason=primary timeout`。时间轴对齐到毫秒：
  `18:01:41` 在 `cc9` 发出 bun 尝试 → `18:03:11` 兜底触发，正好 90s，等于
  `ZEN_FALLBACK_PRIMARY_TIMEOUT_S`；`18:05:40` → `18:07:10` 同理。中间**没有**
  任何 `egress failover` 行，说明既没有换桶、也没有 TTFB 超时：上游把响应
  **响应头**发回来了（所以 `forward.ts` 的 `ZEN_BUN_TTFB_MS` 只管头，永不触发），
  随后正文一个字节都没有。根因在 `_consume_ok`：`stream=true` 走
  `StreamRelay`（有 `STREAM_STALL_S` 看门狗），`stream=false` 走
  `aggregate_sse()`，那条通路**没有任何字节期限**，只被外层 90s 兜底期限
  兜住。于是客户端等 96s 后由一个**不同模型**（GLM-5.3-Flash）回答，而当时
  池里还有健康桶可用。修法：`aggregate_sse(..., stall_s=)` 按
  `STREAM_STALL_S` 约束「下一行正文」的等待，超时抛
  `StreamStalled(ConnectionError)`；换桶循环据此重试另一个桶（非流式客户端的
  wire 上还没有任何字节，重试对客户端不可见）。流式通路保持原语义不变：那里
  字节已经发出去了，只能合成终止符，不能重试。租约释放用独立的
  `kind=stream_stalled`（与 `aggregate_error` 分开，前者是死隧道、后者是坏
  响应体）。证据：`test_aggregate_stall_raises_a_retryable_connection_error`、
  `test_non_stream_stall_blocks_the_bucket_and_retries_elsewhere`。

### Added

- **报告能看见「桶在抖」了**（`egress_probe_flapping`，warning）——24 小时窗口内
  `probe_failed` 113 条，但诊断规则一条都没报，只能靠人工 grep。计数口径是
  **健康→不健康的状态跃迁**（`pool.py` 每次跃迁只记一行），所以高计数＝反复
  抖动、持续宕机只留 1 条，二者不会混。实测：`owin10-telecom` 56、
  `owin10-mobile` 48（这两个是同一台设备 `100.109.109.108` 的两个端口，同向
  抖动＝设备侧问题）、`cc9` 6、`esp32-bucket` 3。动作明确指向设备/隧道，并写明
  **不要**为抖动放宽探针容忍度。证据：
  `test_diagnose_flags_probe_flapping_per_bucket`、
  `test_diagnose_stays_quiet_on_a_single_probe_transition`。
- **日配额 cordon 速率进报告**（`quota_cordons_high`，info）——同一窗口 17 次
  `free_quota_exhausted` / 16 次成功换 IP，这个数字是判断
  `ZEN_EGRESS_ROTATION` 阈值（现为 `*-500`）是否合理的唯一依据。动作栏照抄
  429 归因红线：先做传输栈/body 新鲜度/session 三变量消融，**不得**仅凭 429
  下调阈值。证据：`test_diagnose_flags_quota_cordon_rate_without_touching_rotation`。

## [0.7.6] - 2026-10-28

### Fixed

- **自我优化报告把 12 天账本和 24 小时日志并成一张表，凭空造出 critical** —
  每日巡检报 `[严重] 兜底链自身失败：主链与 proxy_ms 同时不可用`
  （`{"fallback_failed": 125, "incidents": 129}`），退出码 1，按纪律要先修
  proxy_ms。实测证伪：`proxy_ms /healthz` 回 200、`version 3.0.1`、
  `uptime 25572s`、7 个 key；24 小时窗口内 `fallback_failed` 为 **0**，
  最后一条失败在 `2026-10-26T18:42Z`（30 小时前）。根因：账本是整文件读取
  （`tail -c 2000000` 带回 10-16 起的 8631 条），日志是 `--since 24h` 导出的
  255 条，`summarize()` 对两侧不做对齐，于是 12 天历史被当成当前状态。
  修法：`summarize(..., window_hours=)` 以**两侧最新时间戳**为锚把账本裁到窗口内
  （默认 24h，新增 `--events-window-hours`，0 = 不裁），无 `ts` 的记录保留并单独
  计数；锚点取数据不取墙钟，同一输入重放必得同一结论。报告「事件账本」行现在
  打印窗口/锚点/丢弃条数（本次运行：`kept 603 / dropped 8028`）。证据：
  `test_window_events_drops_ledger_history_older_than_the_log_window`、
  `test_stale_ledger_failure_alone_no_longer_raises_a_critical`。
- **TTFB 期限规则读错了相位，既报不出真风险又给错动作** — `ttfb_ms` 在本栈
  量的是中继把**已到手**的首块交给路由的瞬间（实测 0.2–0.3ms），不是上游首字节；
  上游整包缓冲后重放成 SSE 的轮次里它恒为亚毫秒，于是「首块 p95 > 0.8 ×
  `ZEN_TTFB_S`」这条规则在真实负载上**永远不可能触发**（实测首字节前 p95 已达
  63839.8ms，规则沉默）。现在取 `max(ttfb_p95, 首字节前 p95)`——每条流记录只填
  其中一侧，取大才是「客户端见到第一个字节前的最坏耗时」。
- **同一个 63.8s 曾会被建议「调大 ZEN_TTFB_S」，那是反向操作** — 实测
  `17:41:30` 首桶 `owin10-mobile` 发出尝试、`17:42:31`（恰好 60s 后）
  `TtfbTimeout` 换桶、`17:42:34` 由 `owin10-telecom` 200 收尾：期限正常工作，
  是那个桶吃满了它。调大只会让死桶每轮多占 60s+。因此按「窗口内是否有请求内
  换桶」分流：有 → 新增 `head_deadline_absorbed`（info，动作＝查那个桶，
  **不要**调大期限）；无 → 仍报 `ttfb_deadline_tight`（warning）。证据：
  `test_diagnose_separates_a_bucket_eating_the_deadline_from_a_tight_deadline`。

## [0.7.5] - 2026-10-28

### Fixed

- **桌面端实例完全没有兜底链：主链 429 直通客户端** — 生产报障「请求过于频繁，
  请等几秒再发送 / 429 Free usage limit reached」，`request_id=...` 与本机会话
  一致。定位：请求打的是 `http://127.0.0.1:8787`（`~/.workbuddy/models.json`），
  而 `gateway_wb.bat` 里**没有任何 `ZEN_FALLBACK_*`**，`build_adapter` 在不配
  base URL 时根本不包 `FailoverAdapter` —— 兜底链只存在于 ECS 的 `.env`。
  本机单 IP、无出口池，免费额度一穿就无路可走，于是 6 条请求连续
  `status=429 latency_ms≈1.6-2.4s`，客户端只能报「稍后再试」（实际要等次日）。
  修法：本机实例接上 proxy_ms（`http://115.29.231.25:10003`，ModelScope
  `ZhipuAI/GLM-5.3-Flash`）作为备份链，`HOLD_S=300`（额度是整天级的，短窗口
  只会反复撞同一个 429）。验收：主链 429 时日志出现
  `PRODUCTION INCIDENT - FALLBACK ENGAGED` + `FALLBACK ROUTING: model
  'big-pickle' -> 'ZhipuAI/GLM-5.3-Flash'`，`scripts/e2e_workbuddy.py`
  **6/6 通过**（含工具调用、Responses、Anthropic 事件）。
- **兜底 base URL 多写一个 `/v1` 就让整条备份链 404** — 适配器自己拼
  `/v1/chat/completions`，`ZEN_FALLBACK_BASE_URL=http://host:10003/v1` 会拼成
  `/v1/v1/chat/completions`，proxy_ms 回 404，`FailoverAdapter` 判为「备份也
  失败」并回 503 `fallback_backup_error`——现象是「兜底配了却不生效」。
  实测证据：`backup gateway http://115.29.231.25:10003/v1 answered HTTP 404`。
  现在 `config._api_base` 归一化（剥离尾部 `/`、`/v1`，只此一段），两种写法
  等价；8 条参数化测试钉住（含「更深路径不被剥」的反例）。
- **本机常驻任务其实不存在** — 计划任务 `proxy_opencode-wb-gateway` 查无此项，
  8787 是一个早于配置变更的手工进程（`created 01:13:45`，`Stop-Process` 失败后
  仍在服务旧 env）。已重新注册（SYSTEM / AtLogOn / 失败重启 3 次 / 无执行时限）
  并重启，`/healthz` 现在带 `fallback` 状态块。

## [0.7.4] - 2026-10-28

### Fixed

- **请求级错误不再冒充链路故障** — `FailoverAdapter` 原先 `except Exception` 通吃：
  `ContextOverflow`（提示词过长，客户端的问题）被当成主链不可用，于是
  ①记一条 `PRODUCTION INCIDENT - FALLBACK ENGAGED` ②熔断 120s，其间**所有**
  客户端的请求都被送到 proxy_ms ③把同一个超长请求换个模型再答一遍，客户端永远
  学不到「你超长了」。实测证据：`fallback_reason: "ContextOverflow: ... this
  request is about ..."`，随后 60s 内三条正常请求都带 `fallback_reason:
  "hold window"`。现在 `InvalidRequest`（含 `ContextOverflow`）直接抛出，路由层
  回 400 `context_length_exceeded`，兜底链不动。回归测试
  `test_request_level_fault_never_engages_the_backup_chain` /
  `test_invalid_request_also_skips_the_backup_chain`。
- 说明：兜底命中期间（熔断窗口）请求由 proxy_ms 的模型服务，其限额自然是它自己的
  ——上下文守卫因此留在「模型侧」的 zen 适配器里，与所调模型一致。

## [0.7.3] - 2026-10-28

> 主题：模型参数与 opencode 本体对齐 + ECS 侧旧部署彻底清理重建。端口、链路、
> 密钥、对外模型 ID 一律未变（`115.29.231.25:10002` → caddy → `127.0.0.1:10010`，
> 别名 `deepseek-ai/deepseek-v4.1-flash` → `big-pickle`）。

### Added

- **`proxy_opencode/model_profile.py`** — 模型参数的唯一出处，逐项抄自
  `sst/opencode` @ dev（2026-10-27）：仓内 models.dev 快照的限额与能力位、
  `util/token.ts` 的 `len/4` 估算、`session/compaction.ts` 的压缩阈值、
  `session/model-request.ts` 的输出预算。big-pickle：窗口 200000 / 输入 160000 /
  输出 32000，压缩点 **136000**。
- **`proxy_opencode/upstreams/zen/policy.py`** — 三条下发规则：
  ①模型目录没有 `effort` 列表时丢弃客户端的 thinking 控制（big-pickle 即如此，
  客户端本体也没有该调节项，模型按自身上限推理）；
  ②输出预算按 `outputLimit` 补齐/收窄；③提示词超过输入上限时本地拒绝。
- **`ContextOverflow`** — 400 `context_length_exceeded`，与上游同形，因为
  opencode 正是靠这个形状触发压缩（`reason: "overflow"`）；本地拒绝不烧免费额度。
- **新配置**：`ZEN_CONTEXT_GUARD`、`ZEN_CONTEXT_INPUT`、`ZEN_REASONING_POLICY`
  （max|client|off）、`ZEN_OUTPUT_BUDGET`（mirror|clamp|off）。
- **`docs/MODEL_PROFILE.md`** — 参数出处表、压缩点推导、守卫行为、目录暴露口径。
- `/v1/models_cus?format=json` 条目附带 `limit` / `capabilities` /
  `reasoning_efforts` / `compaction_ceiling` / `source`；未知 id 仍为裸字段
  （不给不认识的模型编限额）。`/v1/models` 契约不变。

### Changed

- **ECS 部署形态收敛为「git 工作树即全部」**：删除 `/opt/zen-gw/src` 下的旧快照
  残留（`proxy_opencode.bak-2026*`、`*.egg-info`、`.pytest_cache`/`.mypy_cache`/
  `.ruff_cache`、`runtime-logs`、`.e2e_tmp`、`.e2e_real_gw.log`、`.workbuddy*`）
  与 `/opt/zen-gw/src-backup-*.tgz`；`.env` 与 `data/` 单独备份后原样回填，
  重建镜像 `zen-gw:0.7`（附加版本标签），端口与链路不动。

### Notes

- `logsetup.FIELDS` 增加 `dropped` / `estimated_input` / `max_tokens`：策略动作
  需要留痕，但只记字段名与数字，不记值。

## [0.7.2] - 2026-10-28

> 主题：按关注点重构 + 清理下线物 + 桌面端接入。协议事实未变（AGENTS.md 硬约束 1
> 全部保持不变），门禁由四条收敛为 gateway 三条 + egress-agent 三条。

### Changed

- **refactor(insights): 拆为 `insights/` 包** — `parse`（JSONL 账本 + 文本/JSON 日志，
  含从行头恢复 message）、`metrics`（计数/占比/最近秩分位/相位配对）、
  `diagnose`（规则与阈值）、`render`（报告）。对外仍是 `proxy_opencode.insights`
  的 `analyze/diagnose/...`，调用方无感；规则与阈值集中在 `diagnose`。
- **refactor(egress): `pool.py` 拆出 `models.py`** — `EgressSpec`/`EgressState`/
  `Lease`/配额窗口助手独立成纯数据模块，`pool.py` 只保留选择、cordon、探活与租约记账
  （845 → 678 行）。`from .pool import EgressSpec, Lease, ...` 的既有引用照旧可用。
- **refactor(upstreams): `zen_direct.py`（1166 行）拆为 `upstreams/zen/` 包** —
  按关注点分离，单文件不再同时承担六件事：
  `protocol`（请求头/UA/Retry-After 解析）、`ids`（session/request/workspace 标识）、
  `prompt`（body 门模板与逐请求渲染）、`tools`（形状门工具注入）、
  `body`（请求体组装）、`aggregate`（SSE → 单条 completion）、`errors`（免费层 403
  改写）、`client`（适配器编排：桶级 failover、传输选择、租约释放）。
  prompt/tools 资产移入 `upstreams/zen/assets/`。所有纯函数不再依赖 Settings
  对象，可直接单测；`upstreams/__init__` 只暴露 `zen.ZenDirectAdapter`。
- **refactor(routes): 三条协议路由的响应装配去重** — 新增 `routes/_render.py`
  （`begin` / `streaming` / `json_response`），三处重复的「兜底头 + SSE 注记 +
  生命周期 relay + usage 记录」收敛为一处；此前三份拷贝已经开始漂移（一条路由
  漏注记、另一条跳过 stream 记录）。
- **refactor(docs): 门禁口径同步** — `ruff check proxy_opencode tests scripts`
  （scripts 纳入静态检查）。

### Removed

- **chore: 删除 `balancer/`（旧栈边缘 LB）** — `ARCHITECTURE.md` 早已标注
  「现役栈不部署」，0.7.x 单栈后它是纯维护负担（~700 行 + 33 测试 + 一个 CI job）。
  CI 两个 workflow、AGENTS/CONTRIBUTING/README/OPERATIONS/engineering-constraints
  与代码注释里的引用同步清除；需要时从 git 历史取回。
- **chore: 清理 opc2 下线设备引用** — 示例值/夹具/部署文档改为现役桶名
  （`owin10-mobile` / `owin10-telecom` / `cc9`）；**历史测量记录保留原设备名并标注
  `since-retired`**，不把旧证据改写成现役设备。

### Added

- **feat(client): WorkBuddy 桌面端接入 + 端到端验收** — `docs/CLIENT_WORKBUDDY.md`
  记录桌面版 `~/.workbuddy/models.json` 的格式（数组、字段、热重载、日志判据）与
  本机回环实例（`127.0.0.1:8787`，计划任务 `proxy_opencode-wb-gateway`，
  bun 传输栈）。新增 `scripts/e2e_workbuddy.py`：六项检查覆盖桌面端真实形态
  （别名不泄漏、SSE + 工具 + reasoning、非流式聚合、tool_calls、Responses 与
  Anthropic 事件序列），实测 6/6 PASS。

## [0.7.1] - 2026-10-27

### Added

- **feat(insights): 日志驱动的自我优化闭环** — 新增
  `proxy_opencode/insights.py`（纯函数：解析 / 指标 / 诊断 / 渲染）+
  `scripts/self_optimize.py`（CLI，退出码 1 = 有 critical）+ 设计文档
  `docs/SELF_OPTIMIZATION.md`。把网关自身三源遥测（JSONL 事件账本、
  请求/流日志记录、`/admin/egresses` 快照）折成 11 条确定性诊断规则
  （兜底链自身故障 / 主链抖动 / 截断率 / 静默超时 / 首块期限 / 换 IP 未落地 /
  模型漂移 / 账本未开…），每条带 `evidence` 与按代价排序的 `action`；
  429 归因一律遵守「传输 → prompt → session → 才 IP 配额」红线，不诱导换 IP。
  相位口径（`latency_ms − duration_ms`）按 `request_id` 跨两条日志行配对，
  单行相减会永远缺字段（本模块首版即踩此坑，已改并加测）。
  测试 `tests/test_insights.py`（20 条，含 CLI 退出码与文本/JSON 双格式）。

### Fixed

- **fix(observability): 日志字段白名单补齐兜底归因与写停滞字段** —
  `logsetup.FIELDS` 缺 `fallback`/`fallback_model`/`fallback_reason`
  （`routes/_pipeline.fallback_log_fields()` 每个兜底请求都会携带）与
  `blocked_s`（client-write-stall 警告）、`bytes`/`timeout_s`（failover 首块
  预取 debug 行），两个 formatter 把它们静默丢弃——兜底服务的轮次在日志里
  看起来与主链完全一样，写停滞警告丢失阻塞时长（该坑曾咬过 key_id/ttl_hours，
  见 `test_key_lifecycle_fields_are_rendered` 同类回归）。
  回归测试 `test_fallback_provenance_fields_are_rendered`。

### Changed

- **chore(cleanup): 清理死代码与多余换行** — 删除 `egress/pool.py` 末尾全仓库
  零引用的 `PoolStats` dataclass（`field` 导入一并移除）；`routes/chat.py` 里
  单名 `ServeCompletion` 的 import 去掉无意义的括号换行，3 行变 1 行。
  （同文件里那段同名模块的别名 import 被 ruff/isort 的 `combine-as-imports`
  规则要求单独成段，属规范输出而非冗余，未合并。）

## [0.7.0] - 2026-10-27

> 本节覆盖 v0.5.0 以来 0.6.x→0.7.x 的全部累计变更（107 commits，2026-10-13 →
> 2026-10-27），含 0.7.0a1–a5 五个 alpha。0.7.0 起为生产稳定版。

### Added

- **feat(esp32): 控制通道 HELLO 鉴权（协议契约 bump v4）** — 适配器 :9999 必须公网
  可达（ESP32 从 MiFi NAT 后外连），v3 实测有公网扫描者连入（azenv/SSH 探测），
  且 HELLO 无鉴权＝任何主机可冒充 ESP32 抢占桶槽并收走 CONNECT 流量。v4 在
  `HELLO <name> <lan_ip>` 追加第 4 段共享密钥（固件 `CTRL_SECRET` 常量 / 适配器
  `ESP32_CTRL_SECRET` 环境变量），不匹配回 `BYE` 立即断开；适配器侧密钥为空时
  退回 v3 兼容免鉴权。同批把适配器收编为 ECS systemd 单元
  `esp32-bucket-adapter.service`（此前 nohup 裸进程，无自愈，本次排查中即因
  ssh 会话退出而双实例/全灭过）。实测：假 HELLO 被拒并留痕、真 ESP32 正常注册、
  CONNECT 数据面 2/2 成功。
- **feat(egress): 新增 `egress-agent-esp32/` 模块（ESP32 桶，移动 MiFi 上行）** —
  原 `esp32_p1` 独立仓库合并为子模块：PlatformIO 固件（反向隧道、双网 fallback、
  并发信号量）+ 网关侧适配器（`esp32_bucket_adapter.py`，标准桶接口
  `CONNECT`+`/healthz` :10005）+ MiFi 换 IP 工具（`tools/reboot_mifi.py`）。
  线协议契约冻结 v3 见模块内 `PROTOCOL.md`（改协议先改契约）。
  接入桶池：`ZEN_EGRESS_PROXIES` 追加 `esp32=http://127.0.0.1:10005`。
  实测：CONNECT/HTTP 全链路、8 并发（4 立即+3 排队+1 超时）、断链自愈、
  MiFi 重启后全链路恢复。
- **feat(egress): 按请求数的周期性出口轮换（每桶 500 条成功请求换 IP）** — 此前
  轮换只由硬 429 / 403 触发（即「烧穿之后才换」），运维侧要求在配额耗尽**之前**
  主动换 IP，避免单桶当日成功请求数逼近免费层上限触发 429。新增
  `ZEN_EGRESS_ROTATION`（如 `owin10-mobile=500,owin10-telecom=500,cc9=500`）：
  `counters.record()` 后达到阈值即 `rotation_requested`（trigger=`threshold`，
  ONT 重启换 IP），计数器随成功轮换归零重新武装；与既有机制协同——硬 429 仍
  绕过阈值立即轮换、`HARD_ROTATE_MIN_INTERVAL_S` 下限防抖、失败重试冷却。
  测试 `test_threshold_triggers_rotation_and_resets_counter` /
  `test_threshold_fires_exactly_once_and_resets_on_rotation` /
  `test_settings_parse_rotation_thresholds`。

- **feat(failover): 兜底启用升格为生产事故级记录** — 运维侧要求备用链路接管时
  「醒目提醒 + 日志留痕」。首次跳闸从 WARNING 升为 **ERROR**
  `PRODUCTION INCIDENT - FALLBACK ENGAGED`（INFO 过滤的采集也必留存，
  `PRODUCTION INCIDENT` 前缀可直接 grep 告警）；同一轮故障内的再次跳闸仍是 INFO
  `FALLBACK RE-ENGAGED`（continuation，不重复报事故）；备用链也不可达时 ERROR
  `PRODUCTION INCIDENT - FALLBACK FAILED`；恢复闭合记 WARNING
  `PRODUCTION INCIDENT CLOSED - FALLBACK RECOVERED`。JSONL 事件新增 `severity`
  字段（`incident` / `continuation` / `recovered`），事件账本可按严重度过滤。
  测试 `test_first_trip_is_error_level_production_incident` /
  `test_incident_severity_lands_on_jsonl_events` /
  `test_backup_failure_is_marked_as_incident` /
  `test_re_trip_keeps_continuation_severity`。

### Fixed

- **fix(egress): 探针成功不再清零请求级失败连击（esp32 僵尸桶当日 43/44 失败从未
  cordon）** — `_probe_one` 成功路径无条件把 `failures` 清零并刷 `egress_recovered`
  事件，本意是「agent 恢复后给干净档案」。但 `/healthz` 只反映 agent 控制通道：
  2026-10-27 esp32 桶的 ESP32 控制通道全天在线（探针 30s 一次全绿），其移动 MiFi
  数据面却已劣化（6 次 CONNECT 2 次超时，1MB 下载仅 6.7KB/s），客户端请求 44 次
  首跳 43 次 `502 Tunnel Failed`/`ECONNRESET`。每次探针成功都把请求失败连击在
  凑满 3 之前抹掉，3 连败 cordon **永远无法触发**，least-in-flight 继续把 ~25% 流量
  送进坏桶（当日 44 次 `egress_recovered` 事件刷屏佐证）。现在失败连击只能由
  **成功的请求**（`release(ok=True)`）或**真实的 agent 恢复**（探针从 unhealthy→
  healthy 的跃迁，即控制通道确实断过又回来）清零；`egress_recovered` 事件也随之
  只在跃迁时发一次，不再每 30s 刷屏。测试
  `test_probe_success_keeps_request_failure_streak` /
  `test_probe_recovery_after_outage_resets_and_emits_once`。

- **fix(failover): 备用链路错误不再顶替主链路身份（billing 429 归因错乱）** —
  2026-10-27 00:03Z 客户端收到 `provider: oc1id / layer: billing / retryable:false /
  HTTP 429: insufficient balance`：proxy_ms 的计费 429 被原样中继，客户端读成 zen
  主链路的配额墙并放弃重试，运维还会误判为「该换 IP」。现在 `_via_secondary` 把
  备用错误重塑为网关自有的 `503 fallback_backup_error`（消息只带备用地址与原状态
  码，不回显上游正文），并照旧清零 `_fallback_until` 让下一请求立刻探测主链路。
  测试 `test_backup_error_body_is_not_echoed_to_client` +
  `test_backup_error_relay_releases_hold_for_next_primary_probe`。

- **fix(responses): 生命周期事件名改回 `response.in_progress`** — 流式 `/v1/responses`
  在 `response.created` 之后发的是裸 `in_progress`，而 Responses 线上事件名是
  `response.in_progress`。`event:` 头与 `data.type` 都不匹配任何 SDK 的分派表，
  客户端**静默丢弃**该事件：网关自己的流看着完整，codex 之类客户端却看不到进度
  态。测试 `test_responses_endpoint_streams_masked_events` 现在同时断言正确名存在、
  裸名不存在。

- **fix(routes): 聚合响应体的"合成 SSE"分支补上 `relay_stream`** — `/v1/responses`
  与 `/v1/messages` 在客户端 `stream=true`、上游却返回整包 JSON 时，直接把单块
  SSE 交给 `StreamingResponse`，绕过了 `relay_stream`：因此**没有 `stream ended`
  终态记录**（延迟/结束原因丢失），也没有客户端写停顿看门狗。现在两条路由都走同
  一个包装器，成功率与流生命周期记账和真流式一致。测试
  `test_stream_from_aggregated_body_still_logs_the_stream_end`。

- **fix(egress): 租约释放幂等 + 取消时归还 in-flight 名额** — `Pool.release` 无条件
  `inflight -= 1`，而传输层存在「先 `release` 再抛出」的路径（bun 传输失败后
  `raise ConnectionError`），网关的 `except` 分支又不会再释放一次——两种写法叠加
  会让一次请求的释放被记两次，**把别的请求的 in-flight 名额也减掉**，least-in-flight
  选择因此长期偏斜。`Lease` 增加 `released` 标记，重复判决直接忽略（并记 debug）。
  同时新增 `Pool.abandon(lease)`：客户端中断 / 任务取消这类「桶没有过错」的退出
  只归还名额，不记成功、不记失败、不写事件、不 cordon；适配器在
  `except BaseException` 里调用它再重新抛出（此前的写法会永久泄漏名额，只能靠重启
  进程回收）。测试 `test_release_is_idempotent_for_the_same_lease` /
  `test_abandon_returns_the_slot_without_a_verdict`。

- **fix(egress): 探针容忍非对象 JSON** — `_probe_one` 对 200 响应只做
  `payload = resp.json()`，随后 `payload.get(...)`。agent 返回数组或裸字符串时
  会抛 `AttributeError` 逸出探针循环（与 1c2413d 修掉的「僵尸桶」是同一处代码
  路径的另一种触发形式）。现在非 dict 载荷按空对象处理，走正常的
  healthy/unhealthy 判定。测试 `test_probe_tolerates_non_object_health_payload`。

- **fix(bun): `ZEN_TTFB_S=0` 必须显式导出 `ZEN_BUN_TTFB_MS=0`** — 文档语义是「0
  关闭额外首字节计时器」，但 Python 侧只在 `ttfb_s > 0` 时才设置该环境变量；
  变量缺失让 `forward.ts` 落到默认 `60000`ms，于是"关闭"实际变成"60 秒硬截止"。
  改为无条件导出（含 `"0"`）。测试
  `test_post_without_header_deadline_exports_explicit_zero`（旧用例断言"不导出"，
  即把缺陷写成了契约）。

- **fix(failover): 每请求的 `FALLBACK ROUTING` 从 WARNING 降为 INFO** — 这是一条
  **正常路由**记录（备用链路启用期间每个请求一行），24h 实测 238 行，把真正的
  WARNING 淹在同一级别里。跳闸/恢复的状态跃迁日志保持 WARNING/INFO 原有级别。

- **fix(errors): 只有显式请求错才回 400，其余 ValueError 一律 500** — 管线把适配器
  抛出的**任何** `ValueError` 映射成 `invalid_request_error`，包括上游响应体解析
  意外（线上 2026-10-25 一例：2.2s、`usage=null`、HTTP 400）。后果有两层：把上游
  故障说成"你的请求有错"，且请求级 4xx **不参与桶间 failover**，等于同时损失可用
  性。新增 `errors.InvalidRequest(ValueError)` 作为唯一的客户端错标记
  （`_build_body` / `tools_contract` / `anthropic_proto` 三处校验改用它），
  `dispatch` 仅对 `InvalidRequest` 回 400，其余 `ValueError` 回 500 并记 ERROR
  （不带正文）。同时 `_aggregate_sse` 对非数值 `created` 容忍。测试
  `test_bare_valueerror_from_adapter_is_a_500_not_a_400` /
  `test_invalid_request_still_maps_to_400`。

- **fix(zen): 上游跳增加首字节（响应头）超时** — 生产实测 12h 内 5 次客户端请求
  在 305–315s 完成、其后流本身只需 0.5–4s：`zen_timeout_s=300` 是整个交换的唯一
  上界，黑洞隧道（agent 掉线 / ONT 重启 / CONNECT 不响应）会用掉全部预算才轮到换
  桶。`forward.ts` 新增 `ZEN_BUN_TTFB_MS`（响应头到达即清除该定时器，之后的静默
  交给既有流看门狗），Python 侧 `post()` 的 META 等待也改用该预算兜底，配置项
  `ZEN_TTFB_S`（默认 60s，0 关闭）。同时 `_log_failover` 带上传输失败原因——此前
  300s 悬挂在日志里**零痕迹**，因此长期未被发现。测试
  `test_post_fails_fast_when_no_headers` /
  `test_post_env_carries_header_deadline` /
  `test_ttfb_deadline_default_and_env`。
- **fix(egress): 探针传输失败即判不健康（消灭僵尸桶）** — `_probe_one` 的
  `ConnectTimeout` 会逃逸到探针循环，被 `logger.exception` 打成完整 traceback
  「probe failed unexpectedly」，且 `healthy`/`last_probe_ok` 完全不更新：agent
  已死的桶继续接客，直到 3 次请求级失败（线上出现「10 consecutive failures
  (transport)」连续串与同桶 305s 悬挂）。现在传输异常/未就绪一律落为
  `healthy=false`、`probe_failed` 事件、单行 WARNING（仅按状态跃迁记录），探针
  改用共享连接池。测试 `test_probe_connect_timeout_marks_bucket_unhealthy` /
  `test_probe_failure_logs_once_per_transition`。
- **fix(egress): cordon 只在状态跃迁时记账 + 软 429 缩短租约** — 每个 429 都会
  重新 cordon 到 UTC0 并重发 ERROR 与 `cordon` 事件（单 6h 48 条 ERROR、单日
  ~250 条事件），把唯一可用于归因的事件账本淹掉；且 `Retry-After` 为软限流
  （≤3600s）时同样被禁到当日窗口末，等于一个 30s 节流罚掉一整天容量。现在重复
  判决只静默延长、跃迁才记账，软 429 按 `Retry-After` 设短租约，更短的判决不得
  覆盖更长的存活 cordon。测试 `test_repeated_429_reports_one_cordon_transition` /
  `test_soft_429_cordons_for_retry_after_not_the_whole_day` /
  `test_late_hard_429_does_not_shorten_a_live_cordon`。
- **fix(egress): `expect_prefix` 降级为审计字段** — 前缀是手工维护的白名单，却被
  当作「解除 cordon」与「轮换成功」的硬前置。线上 owin10-telecom 合法出口
  `117.95.178.237`（及多个 `49.x`/`121.234.x`）不在名单内，导致 cordon 永不解除、
  单日 11/21 次轮换被判失败，与 ARCHITECTURE §6「前缀只作审计」自相矛盾。现在 IP
  真的变化即可解除 IP 域 cordon 并接受轮换结果，不符时记 WARNING +
  `prefix_mismatch` 事件，提醒维护名单。`rotate` 仍拒绝「IP 未变」（ISP 重发同址）。
  测试 `test_quota_cordon_lifted_for_out_of_prefix_ip_change` /
  `test_prefix_violation_accepted_and_flagged`。
- **fix(failover): 熔断闭合加迟滞，抑制抖动** — 单次主链路成功即 `FALLBACK
  RECOVERED`、单次失败即 `FALLBACK ENGAGED`，主链路约一半成功率时按请求速率来回
  切换（线上单日 57 engaged / 11 recovered，20 分钟内 6 次往返），每次切换都把
  客户端正在用的模型换成备用网关的 ID。改为连续 N 次成功才闭合
  （`ZEN_FALLBACK_RECOVER_AFTER`，默认 2），同一轮故障内的再次跳闸记为 INFO
  `FALLBACK RE-ENGAGED`（事件保留 `re_trip` 字段）。备用返回错误时释放窗口的
  既有语义不变。测试 `test_recovery_needs_consecutive_successes` /
  `test_failure_resets_the_recovery_streak` /
  `test_re_trip_is_logged_as_continuation`。
- **fix(egress): 免费层 403 触发也受 30 分钟轮换下限约束** — `should_rotate_on_
  free_tier` 此前不看 `HARD_ROTATE_MIN_INTERVAL_S`，形状门若收紧会按 403 频率
  连续重启光猫；body/形状故障换 IP 无效，故与硬 429 路径统一。测试
  `test_free_tier_trigger_respects_the_rotation_floor`。

- **fix(zen): body 门是「prompt 快照新鲜度」而非「真身文本」** — hermes 端到端
  `429 insufficient balance`（备用链路 proxy_ms 计费错误原样透传）+ 主链路三桶
  同时 `429 FreeUsageLimitError`。同出口 IP、同分钟、同 bun 传输、每次新 mint
  session 的 2×2 消融定案：官方 1.19.3 客户端匿名直跑 **200**，网关请求 **429**
  （`retry-after`→UTC0）⇒ 该 IP **没有烧穿**，是 body 门伪装成配额门。UA / 工具集
  / AGENTS 附录单独都不是判别项；**system prompt 的 `Today's date:` 停在抓包当天
  就 429，刷成当天即 200**，模型名与工作区路径属同类「旧抓包重放」信号。修复：
  `zen_prompt_default.txt` 换为 2026-10-25 官方客户端实拍真身（9559B），新增
  `_render_prompt(model)` 每请求重写模型名 / UTC 当天日期 / 工作区路径，
  `_inject_marker()` 传实际发送的上游 ID。实测同桶 **3/3 200**（修复前同一请求体
  4/4 429）。同时确立 **429 归因红线**：四因同形，换 IP 只对真实 IP 配额有效，
  排障顺序 = 传输 → prompt 新鲜度 → session → IP。测试
  `test_prompt_asset_is_a_live_client_capture` /
  `test_render_prompt_binds_model_date_and_workspace` /
  `test_prompt_date_matches_client_date_string` /
  `test_body_marker_renders_requested_model_not_capture_model`。
- **fix(failover): 备用链路返回错误时立即释放保持窗口** — proxy_ms 欠费回
  `429 insufficient balance`，旧行为把后续 120s 流量全压在它上面，客户端判
  `billing/retryable:false` 放弃，而主链路 429 可能只是 body/传输/session 门误伤。
  现在 `_via_secondary` 收到错误中继（非连接失败）时原样回传、记 ERROR
  `FALLBACK FAILED` + JSONL `fallback_failed{status}`，并清零 `_fallback_until`
  让下个请求重新探测主链路；备用**不可达**仍保留熔断。测试
  `test_backup_error_relay_releases_hold_for_next_primary_probe`。
- **fix(zen): 每次上游试探重新 mint 会话（session 单次使用）** — 上游对复用会话
  一律回层 2 同款 429（同 IP：新 mint=200、立即重放=429，且该 IP 随后仍连续服务
  新 mint ≥3 发）。旧 `chat()` 每客户端请求 mint 一次贯穿三条 failover 腿 = 从
  第二腿起重放已消费会话。改为 in-loop 逐试探 `_headers()`；测试
  `test_chat_mints_fresh_session_per_upstream_attempt`。
- **fix(zen): 免费层形状门收紧为「≥4 真名内置工具」** — 1/2/3 真名全 403；4 真名
  200 且 schema/description 内容不校验；混入 1 个改名工具即 403。UA 版本 / 头名
  （含 2.0 系改名）/ `b3` / `traceparent` / `x-opencode-client` 值均不敏感。
  `ZEN_TOOLS_MODE=minimal` 注入数 2→4（`bash,edit,glob,read`）、UA 改
  `1.19.4/ai-sdk 4.0.23/bun 1.4.4`、头集对齐 1.19.4（新增
  `x-opencode-session-id` 双头、去 legacy `x-session-id` 与重复
  `x-opencode-request-id`、`x-opencode-project` 改固定 40-hex）。
- **fix(zen): 兜底耗尽的匿名 403 改回 503 `egress_unavailable`** — 出口级 403
  原样透传会被客户端判 `auth/retryable:false`；轮换约 90s 后全池即可恢复。非门槛
  403 与带 key 模式仍按请求级透传。`zen_direct._egress_gate_answer`。
- **fix(stream): SSE 停顿看门狗 + 租约按结局释放** — 死隧道令流零字节静默满 300s
  读超时，异常穿透 `StreamRelay` 掐断客户端 SSE（hermes `stream_drop`）。
  `STREAM_STALL_S`（默认 60s）超时或中途传输异常 → 合成
  `finish_reason:"length"` + `[DONE]`；关闭钩子携带结局，`stalled`/`interrupted`
  按失败释放（3 连败 cordon），`cut` 与客户端断连仍按 ok。
- **fix(stream): 客户端写停滞看门狗** — `CLIENT_STALL_S`（默认 60s）：生成器停在
  `yield` = ASGI 写被背压阻塞，超阈值即判客户端段死 → WARNING
  `client write stalled`、取消响应任务、释放上游并写终态。监视任务自行完成释放
  与日志（被取消的响应任务会让生成器成环，依赖 close 会拖到 GC）。
- **fix(deploy): 容器启动补 `setup_logging()`** — Dockerfile 走
  `uvicorn --factory`，从未经过 `python -m proxy_opencode`，容器根 logger 无
  handler：全部 INFO 被丢弃、WARNING 走 lastResort 无 extras，无法归因到桶。
- **fix(log): httpx 访问日志静默（红线）** — httpx INFO 行打印完整 URL，池探针
  URL 的 userinfo 携带 agent Basic 凭据 → 泄入容器日志。`setup_logging` 将
  `httpx`/`httpcore` 压到 WARNING，不得回退（
  `test_setup_logging_silences_httpx_access_lines`）。
- **fix(egress): 配额/门槛 cordon 随 ISP 换 IP 解除** — cordon 是对出口 IP 的
  判决：探针观测到 IP 变化、新 IP 在 `expect_prefix` 内且就绪 → `cordon_lifted`；
  外前缀 IP、探针未就绪、transport streak cordon 不解除。
- **fix(egress): `POST /rotate` 传输失败不再立即判失败** — 命令可能已送达或 ONT
  自行重拨；照常在 deadline 内轮询，观测到合法新 IP 即按成功收敛，明确 HTTP 拒绝
  才是最终失败。
- **fix(egress): 僵尸桶 `ready` 语义（两层防御）** — agent `/healthz` 的 `ready`
  曾硬编码 true，出口全灭仍答 ready。agent 端改为
  `egress_probe_ok is not False and 探针新鲜`（`PROBE_READY_GRACE_S=45s`）；网关
  `_probe_one` 另把 `egress_probe_ok is False` 直接判 unhealthy。
- **fix(egress): 选桶轮询均摊** — 原 `min(inflight, name)` 在全空闲时恒取字母序
  最前的桶（顺序请求全落单桶）；改为 `itertools.count()` 轮转 + 稳定
  least-inflight。
- **fix(egress): 免费层 403 `FreeTierError` 按出口 IP 处理** — 释放为
  `free_tier_rejected`（失败）+ 立即 cordon + 绕过阈值换 IP（每窗口每桶一次，
  共用失败冷却）。此前按 ok 释放 → 桶一直"健康"地返回 403。
- **fix(relay): 上游空错误体补标准信封** — 上游偶发 `400 {}`，裸 dict 中继后
  掩码层又注入 `model`，客户端只看到 `400 {"model": ...}`（SDK 显示
  "malformed"）。规则：带 `error` 键原样中继，其余包成 OpenAI 错误信封（裸
  detail/message 截断 200 字符保留）。
- **fix(relay): 静默上游截断合成 `finish=length`** — 显式告知输出上限停止，
  而非协议违规。

### Added

- **feat(failover): zen-direct 出口级故障自动切备用链路 `proxy_ms`** — 主链路撞
  429/5xx/503 出口闸门/传输失败时改由同机 proxy_ms（ModelScope 聚合网关）服务，
  `ZEN_FALLBACK_HOLD_S`（默认 120s）后半开探测切回；请求级 4xx 不切换。实现
  `upstreams/failover.py` 的 `FailoverAdapter`（覆盖三个协议路由 + `/v1/models`），
  `ZEN_FALLBACK_BASE_URL` 空则不包装。三处提醒：WARNING/ERROR 日志、JSONL
  `fallback_*`、`/healthz` 的 `fallback` 块。测试 `tests/test_failover.py` 12 例；
  ECS 实弹非流式/流式均正常。
- **feat(transport): zen 上游一跳改走 bun 子进程** — 上游按 TLS/HTTP 栈判定：
  同一新出口 IP 分钟级对照，bun（boringssl/HTTP2）字节级克隆重放 200、httpx
  （OpenSSL/HTTP1.1）同字节 429（伪装成配额）。实现
  `zen_bun/forward.ts`（stdin body → stdout SSE 直通、stderr 单行
  `META {status,headers}`、hop-by-hop 头剥离、`AbortController` 超时）+
  `upstreams/zen_bun_transport.py`（spawn/环境净化/代理注入/超时/kill + httpx
  响应面鸭子类型）；`zen_direct._exchange` 分发器（bun 优先、httpx 回退，语义与
  日志一致）；镜像预装 bun 1.4.4。`ZEN_BUN_TRANSPORT/ZEN_BUN_BIN/ZEN_BUN_SCRIPT`；
  dev 无 bun 自动回退。测试 `tests/test_zen_bun_transport.py` 13 例。
- **feat(egress): 硬 429 换 IP 30 分钟下限** — `ZEN_EGRESS_ROTATE_MIN_INTERVAL_S`
  （默认 1800，0=关闭），只在**上一次换 IP 成功**后生效（失败重试仍走 15 分钟
  cooldown）。动机：429 四因同形，只有真实配额能靠新 IP 解决；2026-10-25 把非
  配额 429 当配额，单日轮换 32/20/13 次并打散隧道。
- **feat(egress): `onts[].interface` 接受字面 IPv4** — `SIOCGIFADDR` ioctl 只存在
  于 Linux，Windows/Android 写接口名会被静默忽略（两台 ONT 同为
  `192.168.1.1` 时会打错光猫）。新增 `egress.source_address()`（接口名走 ioctl，
  失败回退字面 IPv4）；测试 `test_source_address_literal_ipv4` /
  `test_ont_bind_source_accepts_literal_ipv4`。
- **feat(egress): owin10 单设备双桶接入出口池** — `10001` 有线移动
  （`bind_source 192.168.1.222`）、`10002` 电信 WiFi（`bind_source
  192.168.2.235`），Python 直跑于 `C:\egress-agent`，Task Scheduler `ONSTART`
  SYSTEM 自启；两桶均接 ONT（`huawei` / `huawei_ct`）并实弹轮换成功。
- **feat(egress): Windows 桶部署路径** — `deploy/windows/README.md`（schtasks +
  `bind_source`，含 `win10` 用 `.venv` + Startup 的旧路径对比）。
- **feat(egress): 出口池自动换 IP（P5）** — 每桶日窗 `[08:00, 08:00+1d)`
  Asia/Shanghai，达 `ZEN_EGRESS_ROTATION` 阈值触发；硬 429（`Retry-After >
  3600s` 或无该头）与 403 门槛绕过阈值。编排：标记 rotating → `/rotate` → 轮询
  IP 变化且在前缀内 → 清 cordon、计数归零；计数快照落 `/data` 跨重启存活。
  `/admin/egresses` 增 `counters` 块与手动 `POST /admin/egresses/{name}/rotate`。
- **feat(egress): 结构化事件日志** — `egress/eventlog.py`（JSONL，5MB×2 轮转，
  写失败自动禁用），记录 cordon / 换 IP 前后 IP / 探测到的 IP 变化 /
  `window_rollover` / `free_catalog_refreshed` / `fallback_*`。
- **feat(stream): `STREAM_STALL_S` / `CLIENT_STALL_S` 配置项**（默认 60s，0=关闭；
  事实源 `proxy_opencode/config.py`）。

### Changed

- **chore(egress): 出口池收敛为 owin10 双桶 + cc9** — `win10` 移出（与
  owin10-mobile 同 CGNAT 出口、无独立换 IP 能力），`opc2` 两桶随设备永久下线
  移除；`EXPECT_PREFIX` 补当日实测落段（telecom `121.234.0.0/16`、cc9
  `122.195.0.0/16`、mobile `36.149.0.0/16`），否则换 IP 后 cordon 解不开。
- **chore(zen): 公开模型目录回笼单别名** — 撤第二别名 `opencode/big-pickle`，
  `PUBLIC_MODELS=deepseek-ai/deepseek-v4.1-flash:DeepSeek V4.1 Flash:big-pickle`
  （备份 `.env.bak-singlemodel`）。
- **chore(ops): owin10 Hermes 客户端 provider 收敛为 `oc_uni`** — `openai-api` 是
  Hermes 内置 provider，且 `auth.json` 的 `credential_pool` 会被 backing source
  反复播种（几分钟内复活）。三步清理见 `docs/OPERATIONS.md` §2.2。
- **docs: 文档整体浓缩（本版）** — 同一事实只在一处维护：`ARCHITECTURE.md` 只留
  拓扑/端口/池语义/ADR，`OPERATIONS.md` 只留现状/操作/排障/风险，事故编年压缩为
  表格，`EGRESS_OBSERVABILITY.md` 区分「已落地的决策事件流」与「待落地的逐请求
  账本」，`CHANGELOG` 合并重复小节。

### Docs

- **docs: GitHub 访问方式（`ACCESS.md` §6）** — 远端
  `git@github.com:gitychzh/proxy_opencode.git`，统一 SSH over 443
  （`ssh.github.com:443`，国内 22 端口常被阻断）、`~/.ssh/config` 模板、各机私钥
  归属与工作副本路径、`ssh -T` 验证口径。`gh` CLI 未安装，不要依赖 `gh auth`。
- **docs: Egress 健康判定口径统一** — `healthy = ready 且探针 200`；
  `matches_prefix` 仅作审计字段，不再影响健康。

### Removed

- 旧栈残留：ECS 上的 `zen-lb` / `zen-glm53` / `cloudflared-ingress`（2026-10-17
  清退）、ECS 本机桶（机房 IP 额度即耗）、opc2 双桶（设备永久下线）。
- 文档中的三桶/四桶历史拓扑、"两栈并存"、双桶性能对比与抓包复盘细节（压缩为
  `ARCHITECTURE.md` §10 与 `OPERATIONS.md` §5 的要点）。
