# Egress 可观测性

> 目的：用**结构化 JSONL**回答"某个桶为什么在 14:32 换 IP、前后 IP 是什么、
> 哪个桶在失败"，且**绝不记录**消息内容、prompt、tools、`Authorization` 或任何
> key（AGENTS 硬约束 4）。
>
> **现状（2026-10-25）**：已落地的是 `egress/eventlog.py` 的**决策事件流**
> （cordon / 换 IP / IP 变化 / fallback / 目录刷新）。**逐请求账本
> `egress_request` 尚未实现**，是已知观测性缺口（`OPERATIONS.md` §6）——本文的
> Schema 与统计示例即该缺口的落地规格。

## 1. 已落地：决策事件流

- 写入点：`proxy_opencode/egress/eventlog.py` 的模块单例 `EVENTS.emit(...)`。
- 路径与轮转：`ZEN_EGRESS_EVENT_LOG`（部署为 `/data/egress_events.jsonl`），
  5 MB 大小轮转、保留 `.1`/`.2`；写失败即自动禁用（不影响业务）。
- 每行：`{"ts": "<UTC ISO8601 ms>", "event": "<name>", ...fields}`。

| 事件 | 关键字段 | 含义 |
|---|---|---|
| `cordon` | `egress`, `cordon_reason`, `retry_after_s`, `egress_ip` | 桶被停用；reason= `free_quota_exhausted`（429，绑到窗口末）或 `free_tier_rejected`（403 门槛） |
| `cordon_lifted` | `egress`, `cordon_reason`, `ip_to` | 换 IP 后判决失效 |
| `egress_ip_observed` | `egress`, `previous`, `current`, `matches_prefix` | 探针观测到的 IP 变化（换 IP / ISP 重拨 / 腿回落） |
| `egress_recovered` | `egress` | 探针 unhealthy→healthy 跃迁（agent 恢复；不再随每次成功探针重复发） |
| `request_on_cordoned_bucket` | `egress`, `cordon_reason` | 全池 cordon 时仍派发（降级） |
| `rotation_requested` / `_started` / `_succeeded` / `_failed` / `_cancelled` | `egress`, `trigger`, `ip_from`, `ip_to`, `elapsed_s`, `detail` | 换 IP 全时间线 |
| `rotation_applied` | `egress`, `ip_from`, `ip_to` | 换 IP 后池内状态更新 |
| `window_rollover` / `window_start` | `window` | 日窗滚动（Asia/Shanghai 08:00 = UTC0） |
| `free_catalog_refreshed` | `count`, `ids` | 免费模型目录刷新 |
| `fallback_engaged` / `fallback_recovered` / `fallback_failed` | `secondary`, `status`, `reason` | 备用链路状态变化 |

快速查看：

```bash
tail -f /opt/zen-gw/src/deploy/ecs/data/egress_events.jsonl | jq -r '.'
docker exec zen-gw sh -c "grep cordon_lifted /data/egress_events.jsonl | tail -5"
docker exec zen-gw sh -c "grep rotation_succeeded /data/egress_events.jsonl | tail -5"
```

按桶统计换 IP / cordon 次数（UTC 日）：

```bash
jq -r 'select(.event=="rotation_succeeded" or .event=="cordon")
       | [.ts[:10], .event, .egress] | @tsv' \
  /opt/zen-gw/src/deploy/ecs/data/egress_events.jsonl \
| sort | uniq -c | sort -k2
```

## 2. 待落地：逐请求账本 `egress_request`

### 2.1 为什么需要

只靠决策事件无法回答"每个桶今天服务了多少请求、成功率多少、P95 多少"。要求是
**每桶 + 当时出口 IP** 的逐请求统计，覆盖全部失败形态。

### 2.2 埋点位置

现在的网关没有独立 LB 进程：选桶发生在容器内的 `egress/pool.py`
（`acquire()` 返回 `Lease`），请求收尾在 `zen_direct` 的释放钩子。所以埋点应在
**`Lease` 的生命周期**里完成，而不是边缘 LB 时代的设计：

- `acquire()` 时把 `bucket_name` + **当时**的 `egress_ip` + `inflight_before`
  写入 `ContextVar`（选桶时刻的口径，避免转发中状态变化导致不一致）；
- 释放时（`release(ok, kind=...)`，已带结局）在 `try/finally` 中统一 emit 一行；
- 复用 `EVENTS.emit("egress_request", ...)`，自动获得轮转与失败禁用语义。

### 2.3 Schema（JSONL，一行一事件）

```json
{"ts":"2026-10-25T08:42:15.832+00:00","event":"egress_request","level":"info",
 "request_id":"req_7f43a7e2b3a4","method":"POST","path":"/v1/chat/completions",
 "model":"big-pickle","bucket_name":"owin10-telecom","egress_ip":"121.234.130.245",
 "upstream_status":200,"latency_ms":842,"bytes_recv":512,"inflight_before":1,
 "outcome":"success","fail_category":null,"error_class":null}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | string | 完成时刻，UTC ISO8601。与配额日（UTC0）口径一致 |
| `request_id` | string | 串联同一次客户端请求的多腿 |
| `method` / `path` | string | 路径不含 query/body |
| `model` | string | 顶层 `model` 字段（仅字符串，不解析消息） |
| `bucket_name` | string | **必须**：选中的桶名 |
| `egress_ip` | string | **必须**：选桶时刻该桶出口 IP |
| `upstream_status` | int\|null | 无 HTTP 响应（超时/网络）时 null |
| `latency_ms` | int | `perf_counter()` 计量 |
| `bytes_recv` | int | 接收字节数（带宽观测，可选） |
| `inflight_before` | int | 选桶前该桶在途数（排队分析） |
| `outcome` | enum | `success` / `fail` |
| `fail_category` | enum\|null | 见 §2.4 |
| `error_class` | string\|null | 异常类名，不记堆栈 |

> 红线：不含 `messages`、`content`、`prompt`、`tools` schema、`Authorization`、
> API key、完整请求体。

### 2.4 失败分类

`outcome='fail'` = 未成功返回可用响应。

| 场景 | `fail_category` | `upstream_status` |
|---|---|---|
| 2xx | `null`（success） | 200–299 |
| 429 RateLimit / FreeUsageLimit | `http_429` | 429 |
| 5xx（含 ≥600） | `http_5xx` | 500–599, ≥600 |
| 其它 4xx | `http_4xx_other` | 400–428, 430–499 |
| 超时（Read/Connect/Timeout） | `timeout` | null |
| 连接重置 / EOF / DNS / socket | `network_error` | null |
| 其它（协议、解析等） | `other_error` | null |

统计"所有失败"直接过滤 `outcome=="fail"`。

### 2.5 确保覆盖全部分支

`try/finally` 必须覆盖：成功、HTTP 错误、超时、网络错误、其它异常，以及流式
三条结局（`completed` / `cut` / `stalled` / `interrupted` / `disconnected`）。
流式请求的 `bytes_recv` 用中继累计字节。

## 3. 日志路径与轮转

- **决策事件流**：容器 `/data/egress_events.jsonl`（挂载 `./data:/data`），由
  `EventLog` 自行 5 MB×2 轮转，无需 logrotate。
- **逐请求账本（待落地）**：建议同卷 `/data/egress_requests.jsonl`，由同一个
  `EventLog` 类写入（复用轮转与禁用语义）；若改用宿主机 logrotate，需
  `copytruncate`（长驻 ASGI 进程）。
- **docker 日志**：json-file，8 MB×3，只放 INFO/WARNING 摘要（不含内容）。

## 4. 统计示例（假定 `/data/egress_requests.jsonl`）

每桶总量 / 成功 / 失败 + 当时公网 IP：

```bash
jq -r 'select(.event=="egress_request")
       | [.bucket_name, (.egress_ip // "-"), .outcome] | @tsv' FILE \
| awk -F'\t' '{k=$1"|"$2; t[k]++; if($3=="success") s[k]++}
  END{for(k in t){split(k,a,"|"); f=t[k]-s[k];
      printf "%-16s ip=%-16s req=%6d ok=%6d fail=%6d\n", a[1],a[2],t[k],s[k],f}}'
```

失败分类明细（桶 + IP + 分类）：

```bash
jq -s -r 'map(select(.event=="egress_request" and .outcome=="fail"))
  | group_by([.bucket_name, (.egress_ip//"-"), .fail_category])
  | .[] | [.[0].bucket_name, (.[0].egress_ip//"-"), .[0].fail_category, length] | @tsv' FILE \
| column -t
```

按 UTC 日 × 桶：

```bash
jq -r 'select(.event=="egress_request") | [.ts[:10], .bucket_name, .outcome] | @tsv' FILE \
| sort | uniq -c
```

P50/P95/P99（按桶+IP）：

```bash
python3 - <<'PY'
import json, collections
d = collections.defaultdict(list)
for line in open('/data/egress_requests.jsonl'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('event') == 'egress_request' and isinstance(r.get('latency_ms'), int):
        d[(r.get('bucket_name','-'), r.get('egress_ip') or '-')].append(r['latency_ms'])
for k, v in sorted(d.items()):
    v.sort(); n = len(v)
    print(k[0], k[1], f"n={n}", *(f"p{q}={v[min(n*q//100, n-1)]}" for q in (50, 95, 99)))
PY
```

## 5. 验收清单

- [ ] 每行都带 `bucket_name` + `egress_ip`
- [ ] 选桶时刻的 `egress_ip` 写进上下文（不是转发后重读）
- [ ] `fail_category` 覆盖 §2.4 全枚举，`http_429` 与 `http_4xx_other` 区分
- [ ] 流式结局（stalled/interrupted/cut）正确映射为 fail / success
- [ ] `upstream_status` 在异常时为 null
- [ ] 日志不含 `Authorization`、key、消息体（`tests/test_no_secrets.py` 兜底）
- [ ] 轮转生效，磁盘占用有界
