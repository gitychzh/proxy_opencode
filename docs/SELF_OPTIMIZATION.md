# 自我优化闭环（SELF_OPTIMIZATION）

> 本文件 = **闭环的设计与运行手册**。协议事实见 [`AGENTS.md`](../AGENTS.md)，
> 拓扑/端口见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，运维命令见
> [`OPERATIONS.md`](OPERATIONS.md)，事件字段规格见
> [`EGRESS_OBSERVABILITY.md`](EGRESS_OBSERVABILITY.md)。
> 实现：`proxy_opencode/insights/`（`parse`/`metrics`/`diagnose`/`render` 四个纯函数模块，受测试/ruff/mypy 门禁）+
> `scripts/self_optimize.py`（CLI）。

## 1. 目标与三条不变量

目标：让 WorkBuddy（以及 Hermes）**日常用网关产生的真实流量**成为优化输入——
自己把请求打进去，自己从日志里读出问题，自己改掉低风险项，不需要人来盯仪表盘。

闭环必须始终满足三条不变量，任一条被破坏则闭环暂停并报事故：

| # | 不变量 | 破坏后的表现 | 保障机制 |
|---|---|---|---|
| I1 | **调用不断**：客户端侧永远有可用模型 | 优化/排障期间主链挂掉，客户端直接失败，闭环失去流量输入 | 兜底链 `FailoverAdapter` → proxy_ms（§5） |
| I2 | **归因可读**：每个结论都能追到日志/事件的字段 | 报告说不清为什么，优化变成猜测 | 事件账本 + 请求/流记录 + 诊断规则带 `evidence`（§4） |
| I3 | **红线不破**：不落 prompt/内容/凭据 | 违规且报告本身成为泄漏点 | 三源均只含元数据；`tests/test_no_secrets.py` + 日志字段白名单（§3） |

## 2. 闭环四层

```
① 流量层   WorkBuddy / Hermes ──▶ https://llm.223722.xyz/v1 ──▶ 网关 zen-gw
                                                               │
② 观测层   ┌────────────────────────────────────────────────┬──┴────────────────┬─────────────┐
           │ /data/egress_events.jsonl  (JSONL 事件账本)     │ docker logs        │ /admin/egresses │
           │ cordon/rotation/fallback/window_rollover/ip     │ chat completion    │ 快照            │
           └────────────────────────────────────────────────┴────────────────────┴─────────────┘
                                                               │
③ 分析层   scripts/self_optimize.py ──▶ proxy_opencode/insights/
           parse → summarize（指标）→ diagnose（规则）→ render（报告）
                                                               │
④ 执行层   T0 自动应用（env/配置/文档）  T1 自动改码+过门禁  T2 只报告待人决策
```

## 3. 观测数据契约

三源，**都只含元数据**，可以直接贴进报告：

| 源 | 产生位置 | 关键字段 | 开关 |
|---|---|---|---|
| JSONL 事件账本 | `egress/eventlog.py`（5MB 轮转，保留 2 份） | `event`、`egress`、`cordon_reason`、`ip_from/ip_to`、`reason`、`model`、`severity` | `ZEN_EGRESS_EVENT_LOG=/data/egress_events.jsonl` |
| 请求记录 | `routes/_pipeline.ChatCall.log` | `request_id`、`model`、`status`、`latency_ms`、`usage`、`fallback`、`fallback_model`、`fallback_reason` | `LOG_FORMAT=text|json` |
| 流记录 | `routes/_pipeline.relay_stream` / `StreamRelay` | `request_id`、`chunks`、`ttfb_ms`、`duration_ms`、`reason` | 同上 |
| 快照 | `GET /admin/egresses`（管理鉴权） | `healthy`、`cordon_reason`、`inflight`、`egress_ip`、`counters` | 需 `ADMIN_API_KEYS` |

**两侧窗口必须对齐**：账本是整文件读取（`tail -c 2000000` 会带回数天到十几天的
历史），日志是用 `--since 24h` 导出的，两者默认**不是**同一窗口。0.7.6 之前
`summarize()` 直接计数全部账本记录，于是把 12 天的历史与 24 小时的日志并成一张表，
制造出已经结束的 critical（2026-10-28 实测：`fallback_backup_broken` 由 30 小时前的
`fallback_failed` 触发，而当时 proxy_ms `/healthz` 正回 200、uptime 7 小时）。
现在 `summarize()` 以「两侧最新时间戳」为锚点，把账本裁到 `--events-window-hours`
（默认 24，与 `--since 24h` 对齐）；无 `ts` 的记录不丢弃、单独计数（无法归属不等于
没有发生）。锚点取数据而非墙钟，同一份输入重放必得同一结论。报告的「事件账本」
行会打印窗口、锚点与丢弃条数。

**两侧时钟也必须对齐**（0.7.8）：本机桌面实例的**文本日志打的是本地时间**（`LOG_FORMAT=text`
由 uvicorn 默认 formatter 出头），而事件账本是 UTC。`record_epoch()` 把无时区的时间
按 UTC 解释，于是本地 UTC+8 的日志会让锚点**超前 8 小时**：`--events-window-hours 5`
把整本账本裁光、诊断报「账本可能未开启」，兜底故障全部看不见（2026-10-28 round 3
实测：53 条账本被裁到 0，含 7 条 `fallback_failed`）。CLI 因此提供
`--log-tz {utc,local}`：`local` 从本机 `astimezone().utcoffset()` 取偏移并从日志
锚点中扣除。容器日志（`docker logs`）是 UTC，保持默认 `utc`。

**字段白名单是硬门**：`logsetup.FIELDS` 是 `extra` 进入日志的唯一通道，未列入的字段
被两个 formatter 静默丢弃。0.7.1 补入了 `fallback*` / `blocked_s` / `bytes` /
`timeout_s`——在此之前每个兜底轮次在日志里看起来与主链完全一样。新增 `extra` 字段
必须同时改 `FIELDS` 与 `tests/test_logging.py`。

**流结束归因**（`reason` 取值，`StreamRelay` 权威）：

| reason | 含义 | 是否上游故障 |
|---|---|---|
| `completed` | 上游给了终止 chunk | 否 |
| `cut` | 上游干净关闭但无终止 chunk（已合成 `finish_reason:length` + `[DONE]`） | 上游截断 |
| `stalled` | 超过 `STREAM_STALL_S` 无字节（看门狗触发） | 出口链路 |
| `interrupted` | 流中传输异常 | 出口链路 |
| `client_disconnected` / `client_stalled` | 客户端断开 / 写停滞（`CLIENT_STALL_S`） | **否**，客户端侧 |

## 4. 指标与诊断规则

`summarize()` 产出指标块，`diagnose()` 按**严重度 → 代价从低到高**排序输出
`{severity, key, title, evidence, action}`。规则表（阈值是 `insights/diagnose.py` 模块常量）：

| key | 触发条件 | 严重度 | 动作方向 |
|---|---|---|---|
| `fallback_backup_broken` | 账本出现 `fallback_failed` | **critical** | 先修 proxy_ms（余额/可达/密钥）——兜底链自身坏了，闭环失去 I1 |
| `primary_unstable` | `fallback_engaged ≥ 3` | warning | 按 429 归因红线顺序：传输栈 → prompt 新鲜度 → session → 才怀疑 IP 配额 |
| `stream_cut_high` | `cut_ratio > 20%` | warning | 查 body 门（prompt 的日期/模型名/工作区是否本请求现场渲染）与 `max_tokens` |
| `stream_stalled_high` | `stalled_ratio > 5%` | warning | 查桶 probe/连接超时；未排除传输前不要把 429 当配额 |
| `stream_undetermined_high` | 无收尾记录占比 > 10% | warning | 查容器重启/OOM 与日志`--since` 窗口对齐 |
| `ttfb_deadline_tight` | 首块 p95（`ttfb_ms` 与「首字节前」取大）> 0.8 × `ZEN_TTFB_S`，且窗口内**没有**请求内换桶 | warning | 调 `ZEN_TTFB_S` 或先查最慢桶（别拿调大期限遮住黑洞跳） |
| `head_deadline_absorbed` | 同上比例，但窗口内有请求内 `egress failover` | info | **不要**调大 `ZEN_TTFB_S`（只会让死桶多占更久）；查吃满期限的那个桶 |
| `pre_byte_slow` | 首字节前耗时 p95 > 20s | info | 对比 `ZEN_FALLBACK_PRIMARY_TIMEOUT_S`；这段看门狗看不见 |
| `rotation_not_landing` | 有 IP 相关 cordon 且 `rotation_failed > 0` | warning | 核对 ONT 配置/prefix 与 agent `/rotate` |
| `quota_burn_no_rotation` | 有 `free_quota_exhausted` 但无成功换 IP | info | 确认 `ZEN_EGRESS_ROTATION` 已配（阈值可在烧穿前主动换） |
| `no_events` / `no_traffic` | 账本 0 条 / 请求 0 条 | info | 开启事件账本 / 先跑一轮真实流量 |
| `fallback_model_drift` | 兜底实际模型 ≠ `--fallback-model` | info | 核对 `ZEN_FALLBACK_DEFAULT_MODEL` / `ZEN_FALLBACK_MODEL_MAP` |
| `egress_probe_flapping` | 单桶 `probe_failed` 跃迁 ≥ 12/窗口 | warning | 设备侧上行/隧道（`agent not ready`、`ConnectTimeout`），**不要**放宽探针容忍度掩盖它 |
| `quota_cordons_high` | `free_quota_exhausted` cordon ≥ 10/窗口 | info | 先做三变量消融，再评估 `ZEN_EGRESS_ROTATION` 阈值；**不得**仅凭 429 下调 |

`probe_failed` 的计数口径是**健康→不健康的状态跃迁**（`egress/pool.py` 每次跃迁只写
一行，持续宕机不重复记），所以高计数＝反复抖动、长期宕机只留 1 条：这条区分是
`egress_probe_flapping` 能成立的前提，改日志口径前必须先改这条规则。

**相位口诀的落地**：`latency_ms`（请求记录）− `duration_ms`（流记录）≈ 首字节前耗时。
两条记录**不同行**，必须按 `request_id` 配对——`insights.summarize()` 先建
`request_id → latency_ms` 索引再配对，单行相减会永远缺一个字段。

`ttfb_ms` 在本栈里结构性地接近 0（实测 0.2–0.3ms，而同一轮 `latency_ms` 3098ms、
`duration_ms` 340ms）：它量的是中继把**已经到手的**首块交给路由的瞬间，不是上游
首字节。因此判期限只能取 `max(ttfb_p95, 首字节前 p95)`——每条流记录只填其中一侧
（真流式填 `ttfb`，上游整包缓冲后重放成 SSE 的填「首字节前」），取大才是「客户端
见到第一个字节前的最坏耗时」。

## 5. 兜底链：闭环不中断的保证（I1）

实现在 `upstreams/failover.py`，配置在 `config.py`（`ZEN_FALLBACK_*`），
装配在 `upstreams.build_adapter`（`zen_fallback_base_url` 非空才包一层）。
所有三个协议路由 + `/v1/models` 都走 `app.state.adapter`，所以**一个包装覆盖全部面**。

**触发**：主链抛异常（传输/超时/adapter 故障）或上游错误中继 `status ≥ 400`，
但排除 `_PASSTHROUGH_STATUSES = {400, 401, 404, 422}`——请求本身错了，换厂商也一样错，
重试只会翻倍客户延迟。

**期限**：`ZEN_FALLBACK_PRIMARY_TIMEOUT_S`（默认 90s）包住整次主链尝试；
`ZEN_FALLBACK_FIRST_CHUNK_TIMEOUT_S`（默认 30s）预取首个 SSE 块——**首块未到就切换，
客户端一个字节都没见过**，切换对它不可见。首块一旦交出去，本轮即已承诺：中途失败
交给流看门狗，绝不静默重试（会重复输出）。

**熔断**：触发后 `ZEN_FALLBACK_HOLD_S`（默认 120s）内全部直走备份；窗口后首个请求
回探主链（半开）；连续 `ZEN_FALLBACK_RECOVER_AFTER`（默认 2）次成功才宣布恢复——
迟滞是为了避免"主链一半请求成功"时 engaged/recovered 抖动（实测 2026-10-25 一天
57 engaged / 11 recovered，且每次抖动都换掉客户端看到的模型）。

**可观测（不可静默）**：
- 首次触发 = `ERROR "PRODUCTION INCIDENT - FALLBACK ENGAGED"`（INFO 过滤也删不掉）
  + JSONL `fallback_engaged{severity: incident}`；同一轮故障内的再触发是
  `severity: continuation`（INFO），恢复发 `fallback_recovered{severity: recovered}`
- 响应头 `X-Gateway-Fallback` / `-Model` / `-Reason`；流式响应首行带 SSE 注释
  `: gateway-fallback secondary=... model=... reason=...`（任何 SSE 解析器都忽略）
- `/healthz` 的 `fallback` 块：`engaged` / `hold_remaining_s` / `model`

**备份自身失败的处理（必须是这个形状）**：proxy_ms 自己 429（余额）或不可达时，
**不得**把它的错误原样转发——Hermes 会把 ModelScope 的计费 429 读成主链配额耗尽，
判 `retryable:false` 直接停，运维还会去换一个根本治不好的 IP。网关改写成自己的
可重试 `503 fallback_backup_error`，写明原上游状态，并**释放熔断**让下一请求回探主链
（主链的 429 可能根本不是配额——四种成因同形）。

生产配置（ECS `deploy/ecs/.env`）：

```dotenv
ZEN_FALLBACK_BASE_URL=http://172.17.0.1:10003/v1
ZEN_FALLBACK_API_KEY=<proxy_ms CLIENT_KEYS 值>
ZEN_FALLBACK_DEFAULT_MODEL=ZhipuAI/GLM-5.3-Flash
ZEN_FALLBACK_MODEL_MAP=big-pickle=ZhipuAI/GLM-5.3-Flash
ZEN_FALLBACK_HOLD_S=120
ZEN_FALLBACK_RECOVER_AFTER=2
ZEN_FALLBACK_PRIMARY_TIMEOUT_S=90
ZEN_FALLBACK_FIRST_CHUNK_TIMEOUT_S=30
```

> 注意映射键是**路由层解析后的上游 id**（`big-pickle`），不是对外别名
> `deepseek-ai/deepseek-v4.1-flash`——包装层收到的 body 已经过 `registry.resolve`。

## 6. 执行分级（谁能改什么）

| 级别 | 范围 | 谁做 | 门禁 |
|---|---|---|---|
| **T0 自动应用** | `.env` 调参（`ZEN_TTFB_S`、`STREAM_STALL_S`、`ZEN_EGRESS_ROTATION`、`ZEN_FALLBACK_*`）、日志字段白名单、文档 | WorkBuddy 直接改 + 重启容器 | 改后必须跑一轮真实流量验证 |
| **T1 改码** | `insights/` 规则、转换器、路由、测试 | WorkBuddy 改 + 跑全部门禁 | `pytest tests/` + `ruff check proxy_opencode tests scripts` + `mypy proxy_opencode`（+ egress-agent 侧三项） |
| **T2 只报告** | 协议常量（session 编码、UA、prompt 资产）、熔断语义、出口池判据、任何会改变对外契约的项 | 只写报告，等人决策 | 需 §7 的完整流量验证 |

判据：**能靠一次容器重启回滚的进 T0；需要改代码才能回滚的进 T1；改错会让上游
直接 403/429 的进 T2**（协议事实层最贵，AGENTS.md 硬约束 1）。

## 7. 运行手册

### ECS 上（推荐：就近读，无需外传日志）

```bash
ssh -p 222 root@115.29.231.25
cd /opt/zen-gw/src
python3 scripts/self_optimize.py \
  --events /data/egress_events.jsonl \
  --log-cmd "docker logs zen-gw --since 24h 2>&1" \
  --ttfb-s "${ZEN_TTFB_S:-60}" \
  --fallback-model "ZhipuAI/GLM-5.3-Flash" \
  --out /data/self_opt_report.md
echo "exit=$?"     # 1 = 有 critical（兜底链自己坏了），需立即处理
```

容器镜像内没有 `python3`+仓库时，用仓库视角跑：

```bash
docker run --rm --network host -v /opt/zen-gw/src:/src -v /data:/data \
  -w /src python:3.12-slim python scripts/self_optimize.py \
  --events /data/egress_events.jsonl --log /dev/null
```

### 工作站（先拉两源，再离线分析）

```bash
ssh -p 222 root@115.29.231.25 'tail -c 2000000 /data/egress_events.jsonl' > /tmp/events.jsonl
ssh -p 222 root@115.29.231.25 'docker logs zen-gw --since 24h 2>&1' > /tmp/gw.log
python scripts/self_optimize.py --events /tmp/events.jsonl --log /tmp/gw.log \
  --fallback-model "ZhipuAI/GLM-5.3-Flash"

# 本机桌面实例（日志是本地时间，账本是 UTC）——必须传 --log-tz local
python scripts/self_optimize.py \
  --events C:/ProgramData/proxy_opencode/egress_events.jsonl \
  --log C:/ProgramData/proxy_opencode/gateway.log \
  --events-window-hours 5 --log-tz local \
  --fallback-model "ZhipuAI/GLM-5.3-Flash"
```

### 定时（生产建议：每天 09:00 Asia/Shanghai，UTC 配额重置后一小时）

```ini
# /etc/systemd/system/zen-selfopt.service
[Unit]
Description=proxy_opencode self-optimization report
[Service]
Type=oneshot
WorkingDirectory=/opt/zen-gw/src
ExecStart=/usr/bin/python3 scripts/self_optimize.py \
  --events /data/egress_events.jsonl \
  --log-cmd "/usr/bin/docker logs zen-gw --since 24h 2>&1" \
  --out /data/self_opt_report.md
```

```ini
# /etc/systemd/system/zen-selfopt.timer
[Unit]
Description=Daily self-optimization report
[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

`systemctl enable --now zen-selfopt.timer`。WorkBuddy 侧用一条每日自动化读
`/data/self_opt_report.md` 并执行 T0/T1 项，见 §8。

## 8. WorkBuddy 的循环职责

1. **产生流量**：日常编码/问答走 `https://llm.223722.xyz/v1`（别名
   `deepseek-ai/deepseek-v4.1-flash`），本身就是闭环输入。
2. **读报告**：拉 `/data/self_opt_report.md`（或本地跑 CLI）。
3. **执行**：按 §6 分级——T0 直接改 `.env` 并 `docker compose up -d`；T1 改代码 +
   全门禁 + 提交；T2 只把结论写进报告。
4. **验证**：任何 T0/T1 动作后，打一轮真实请求（`scripts/e2e_protocols.py` 或
   WorkBuddy 自用），确认 `cut_ratio`/`stalled_ratio`/`fallback_served` 没变差。
5. **留痕**：结论与动作写 `.workbuddy/memory/YYYY-MM-DD.md`，跨会话可追。

## 9. 失败模式与对策

| 失败模式 | 症状 | 对策 |
|---|---|---|
| 账本未开 | 报告只有 `no_events` | 设 `ZEN_EGRESS_EVENT_LOG`，确认 `/data` 对 uid 10002 可写（否则 eventlog 静默禁用） |
| 日志窗口错位 | `stream_undetermined_high` 升高 | `docker logs --since` 与事件窗口对齐；eventlog 5MB 轮转，`tail -c` 取尾部 |
| 兜底链自身挂了 | `fallback_backup_broken`（critical） | 修 proxy_ms 余额/可达性；期间主链故障直达客户端 |
| 报告"全绿"但实际有故障 | 规则没覆盖 | 补规则进 `insights/diagnose.py` + `tests/test_insights.py`（纯函数，可用合成样本证伪） |
| 把 429 一律当配额 | 换 IP 风暴（实测单日 32/20/13 次） | `primary_unstable` 的 action 强制归因顺序；`rotation_not_landing` 显式提示换 IP 只对真实配额有效 |
| 优化动作引入回归 | 门禁红 | 门禁四条任一红即回滚；T2 项永不由闭环自动改 |

## 10. 明确未做（不进闭环自动化）

- **自动 apply 协议常量**（session 编码、UA、prompt 资产、bun 版本）：改错直接 403/429，
  属 T2，永远只报告。
- **自动重启 ONT / 换 IP 的决策**：已有 `EgressRotator` 按阈值与硬 429 触发；
  闭环只读它的结果（`rotation_*` 事件），不新增触发源。
- **`egress/updater.py`（P4 release watcher）**：当前只有设计文档、无实现
  （`ARCHITECTURE.md` 标注"未开始"）。闭环依赖它之前必须先落地，否则 T2 报告里
  "上游协议可能已变"的判断没有自动输入。
