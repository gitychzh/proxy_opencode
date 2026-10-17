# Egress 可观测性（egress_request 日志）

> 目的：用**结构化 JSONL**记录“请求-路由-结果”，实现**每桶 + 当时公网 IP**的精确逐请求统计（成功/失败全覆盖），且不记录消息内容、prompt 或任何 key。

## 1. 设计原则

- **LB 是唯一归属点**：负载均衡器（`balancer/lb.py`）是选桶（bucket）的唯一位置。只有选桶时才知道 `bucket_name` + 当时的 `egress_ip`。因此日志埋点必须在 LB 内完成。
- **零敏感信息**：严格遵守 `AGENTS.md` 日志红线——不记录 `messages`、`content`、`prompt`、`tools`、`Authorization`、API Key 等任何用户输入或密钥。
- **结构化 JSONL**：每条请求一行，UTC 时间戳，便于 `jq`、`python`、`awk` 直接统计。
- **失败全覆盖**：按要求区分 `success`、`http_429`、`http_5xx`、`timeout`、`network_error`、`http_4xx_other`、`other_error`。
- **最小侵入**：只新增日志，不改变现有路由、均衡或熔断逻辑。

## 2. 日志 Schema（JSONL）

文件路径（建议）：`/var/log/zen-gw/requests.jsonl`（容器内）通过宿主机卷挂载持久化：`deploy/ecs/docker-compose.yml` 中 `./logs:/var/log/zen-gw:rw`。

每行一个 `egress_request` 事件：

```json
{
  "ts": "2026-10-17T08:42:15.832Z",
  "event": "egress_request",
  "level": "info",
  "request_id": "req_7f43a7e2b3a4",
  "method": "POST",
  "path": "/v1/chat/completions",
  "model": "big-pickle",
  "bucket_name": "opc2-eth",
  "egress_ip": "223.68.27.11",
  "upstream_status": 200,
  "latency_ms": 842,
  "bytes_sent": 128,
  "bytes_recv": 512,
  "inflight_before": 1,
  "outcome": "success",
  "fail_category": null,
  "error_class": null,
  "client_ip": "10.0.0.5"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | string(UTC, ISO8601/Z) | 请求完成时刻（UTC）。与配额日（UTC 0 点）统计口径一致。 |
| `event` | string | 固定 `egress_request`，便于过滤。 |
| `level` | string | `info`（成功/失败统一用 info）。 |
| `request_id` | string | 请求追踪 ID（可选）。用于串起同一请求链路。 |
| `method` | string | HTTP 方法（`POST/GET` 等）。 |
| `path` | string | 请求路径，不含 query string、不含 body。 |
| `model` | string | 安全提取的顶层 `model` 字段（仅字符串，不解析消息内容）。 |
| `bucket_name` | string | **必须**：LB 选中的桶名（`opc2-wifi`、`opc2-eth`、`cc9` 等）。 |
| `egress_ip` | string | **必须**：选桶时刻该桶的出口公网 IP（`egress_ip`）。 |
| `upstream_status` | int\|null | 上游 HTTP 状态码。网络异常/超时等无 HTTP 响应时为 `null`。 |
| `latency_ms` | int | 整体耗时（ms）。用 `time.perf_counter()` 计量。 |
| `bytes_sent` | int | 发送字节数（可选，用于带宽观测）。 |
| `bytes_recv` | int | 接收字节数（可选，用于带宽观测）。 |
| `inflight_before` | int | 选桶前该桶的 `inflight` 值（可选，用于排队分析）。 |
| `outcome` | enum | `success` 或 `fail`。 |
| `fail_category` | enum\|null | 失败分类（见 §3）。成功时 `null`。 |
| `error_class` | string\|null | 异常类名（如 `TimeoutError`、`ConnectError`、`HTTPStatusError`），用于排障，不记录堆栈。 |
| `client_ip` | string\|null | 客户端 IP（最接近网关的一跳）。若不需来源分析可省略。 |

> **红线**：绝不记录消息内容、prompt、tools schema、`Authorization`、API Key、完整请求体。

## 3. 失败分类规则

`outcome='fail'` 定义为“请求未成功返回可用响应”（限流、服务端错误、网络异常、超时等）。

| 场景 | `outcome` | `fail_category` | `upstream_status` |
|---|---|---|---|
| 上游返回 2xx（200–299） | `success` | `null` | 200–299 |
| 上游返回 429（RateLimit/FreeUsageLimit） | `fail` | `http_429` | 429 |
| 上游返回 5xx/6xx（500–599、>=600） | `fail` | `http_5xx` | 500–599, >=600 |
| 上游返回 4xx（非 429）（400–428、430–499） | `fail` | `http_4xx_other` | 400–428, 430–499 |
| 读/连接超时（`TimeoutError`、`ReadTimeout`、`ConnectTimeout` 等） | `fail` | `timeout` | `null` |
| 网络错误（连接被重置、EOF、Broken pipe、DNS 失败、Socket 错误、`ConnectionError`、`OSError`） | `fail` | `network_error` | `null` |
| 其他异常（协议错误、JSON 解析异常等非预期） | `fail` | `other_error` | `null` |

> 统计“所有失败的请求”时，直接过滤 `outcome == 'fail'` 即可（涵盖 429/5xx/timeout/network_error/http_4xx_other/other_error）。

## 4. 埋点建议（balancer/lb.py）

核心思路：**选桶成功时把上下文写入 `ContextVar`，转发全程用 `try/finally` 统一写日志**。

### 4.1 ContextVar

```python
import time, json, os, uuid
from contextvars import ContextVar
from datetime import datetime, timezone

req_ctx = ContextVar("req_ctx", default={})
LOG_PATH = "/var/log/zen-gw/requests.jsonl"
```

### 4.2 选桶时记录

在 `select_upstream`（或选中 bucket 后）立即设置：

```python
async def select_upstream(...):
    u, st = await picker.pick()
    req_ctx.set({
        "bucket_name": u.name,
        "egress_ip": getattr(u, "egress_ip", None) or getattr(st, "egress_ip", None),
        "inflight_before": getattr(st, "inflight", 0),
    })
    return u, st
```

> 用“选桶时刻”的 `egress_ip`，避免转发过程中状态变化导致口径不一致。

### 4.3 异常分类与统一 emit

在转发主路径用 `try/finally`：

```python
def _classify_exc(exc):
    # httpx/urllib 风格按实际库调整
    if isinstance(exc, TimeoutError):
        return ("fail", "timeout", type(exc).__name__)
    # NetworkError/ConnectError/OSError 等
    # HTTPStatusError 单独处理（有 status_code）
    return ("fail", "other_error", type(exc).__name__)

def _extract_model(body: bytes | None) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body)
        m = data.get("model")
        return m if isinstance(m, str) else None
    except Exception:
        return None

def _emit_log(ctx, method, path, model, status, dt_ms, outcome, fcat, errcls, bs, br, client_ip):
    rec = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "event": "egress_request",
        "level": "info",
        "request_id": uuid.uuid4().hex[:16],
        "method": method,
        "path": path,
        "model": model,
        "bucket_name": ctx.get("bucket_name"),
        "egress_ip": ctx.get("egress_ip"),
        "upstream_status": status,
        "latency_ms": dt_ms,
        "bytes_sent": bs or 0,
        "bytes_recv": br or 0,
        "inflight_before": ctx.get("inflight_before"),
        "outcome": outcome,
        "fail_category": fcat,
        "error_class": errcls,
        "client_ip": client_ip,
    }
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # 日志写入失败不影响业务
        pass
```

转发示例：

```python
t0 = time.perf_counter()
status = None; outcome="success"; fcat=None; errcls=None; bs=0; br=0
try:
    # 转发（成功时拿到 resp.status_code 与 bytes_recv）
    resp = await client.send(req, stream=False)
    status = resp.status_code
    br = len(resp.content) if hasattr(resp, "content") else 0
    # ...
except httpx.HTTPStatusError as e:
    status = e.response.status_code
    if status == 429:
        outcome,fcat,errcls = "fail","http_429", type(e).__name__
    elif 500 <= status < 600:
        outcome,fcat,errcls = "fail","http_5xx", type(e).__name__
    elif 400 <= status < 500 and status != 429:
        outcome,fcat,errcls = "fail","http_4xx_other", type(e).__name__
    else:
        outcome,fcat,errcls = "fail","other_error", type(e).__name__
    raise
except (httpx.TimeoutException, asyncio.TimeoutError) as e:
    status=None; outcome,fcat,errcls="fail","timeout", type(e).__name__
    raise
except (httpx.NetworkError, httpx.ConnectError, OSError, IOError) as e:
    status=None; outcome,fcat,errcls="fail","network_error", type(e).__name__
    raise
except Exception as e:
    status=None; outcome,fcat,errcls="fail","other_error", type(e).__name__
    raise
finally:
    dt_ms = int((time.perf_counter()-t0)*1000)
    _emit_log(req_ctx.get({}), method, path, model, status, dt_ms, outcome, fcat, errcls, bs, br, client_ip)
```

## 5. 部署：日志路径、轮转、挂载

### 5.1 容器挂载（deploy/ecs/docker-compose.yml）

```yaml
services:
  zen-gw:
    # ...
    volumes:
      - ./logs:/var/log/zen-gw:rw
      - ./data:/data:rw
```

### 5.2 宿主机目录

```bash
mkdir -p /opt/zen-gw/src/deploy/ecs/logs
chmod 755 /opt/zen-gw/src/deploy/ecs/logs
```

### 5.3 logrotate（宿主机 ECS）

新增 `/etc/logrotate.d/zen-gw-requests`：

```conf
/var/log/zen-gw/requests.jsonl {
    daily
    rotate 30
    compress
    missingok
    notifempty
    copytruncate
    create 0640 root root
    dateext
}
```

说明：

- `daily`：按天切割，便于按 UTC 日统计。
- `rotate 30`：保留最近 30 天。
- `copytruncate`：安全切割，不中断长时 ASGI 进程写入。
- `compress`：节省磁盘空间。

## 6. 统计示例（jq + python）

以下示例均假定日志位于 `/var/log/zen-gw/requests.jsonl`。可改成本地路径。

### 6.1 每桶请求量（总/成功/失败）+ 当时公网 IP

```bash
jq -r '
  select(.event=="egress_request")
  | [.bucket_name, (.egress_ip // "-"), .outcome]
' /var/log/zen-gw/requests.jsonl \
| awk -F'\t' '
{
  k=$1"|"($2==""?"-":$2)
  total[k]++
  if($3=="success") ok[k]++
  else fail[k]++
}
END{
  for(k in total){
    split(k,a,"|"); b=a[1]; ip=a[2]
    printf "%-12s ip=%-15s req=%6d ok=%6d fail=%6d ok_rate=%.1f%% fail_rate=%.1f%%\n", b, ip, total[k], ok[k], fail[k], ok[k]*100/total[k], fail[k]*100/total[k]
  }
}'
```

### 6.2 失败分类明细（按桶+公网 IP+分类）

```bash
jq -s -r '
  [.[] | select(.event=="egress_request" and .outcome=="fail")]
  | group_by([.bucket_name, (.egress_ip // "-"), .fail_category])
  | map({bucket:.[0].bucket_name, ip:.[0].egress_ip // "-", cat:.[0].fail_category, c:length})
  | (["bucket","ip","fail_category","count"], (.[] | [.bucket,.ip,.cat,.c]))
  | @tsv
' /var/log/zen-gw/requests.jsonl | column -t -s$'\t'
```

### 6.3 按 UTC 日期统计（三桶每日处理量+成功率）

```bash
jq -r '
  select(.event=="egress_request")
  | (.ts[:10]) as $d
  | [$d, .bucket_name, (.egress_ip // "-"), .outcome]
' /var/log/zen-gw/requests.jsonl \
| awk -F'\t' '
{
  k=$1"|" $2"|" ($3==""?"-":$3)
  t[k]++; if($4=="success") s[k]++
}
END{
  for(k in t){
    split(k,a,"|"); d=a[1]; b=a[2]; ip=a[3]
    fail=t[k]-s[k]
    printf "%s %-12s ip=%-15s req=%6d ok=%6d fail=%6d ok_rate=%.1f%%\n", d,b,ip,t[k],s[k],fail,s[k]*100/t[k]
  }
}' | sort
```

### 6.4 按桶统计 P50/P95/P99 时延

```bash
python3 <<'PY'
import json
from collections import defaultdict

data = defaultdict(list)
with open('/var/log/zen-gw/requests.jsonl') as f:
    for l in f:
        if not l.strip():
            continue
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get('event') != 'egress_request':
            continue
        k = (r.get('bucket_name') or '-', r.get('egress_ip') or '-')
        lm = r.get('latency_ms')
        if isinstance(lm, int):
            data[k].append(lm)

def p(v, q):
    if not v: return 0
    idx = int(len(v) * q // 100)
    return v[idx]

for (b,ip),v in sorted(data.items()):
    v.sort()
    print(f"{b:12s} ip={ip:15s} n={len(v):6d} p50={p(v,50):4d} p95={p(v,95):4d} p99={p(v,99):4d} ms")
PY
```

### 6.5 实时查看最新日志

```bash
tail -f /var/log/zen-gw/requests.jsonl | jq -r '.'
```

### 6.6 统计“所有失败”（含429/5xx/timeout等）

```bash
jq -r '
  select(.event=="egress_request" and .outcome=="fail")
  | [.bucket_name, (.egress_ip // "-"), .fail_category, (.upstream_status // "-")]
' /var/log/zen-gw/requests.jsonl | wc -l
```

## 7. 校验清单

- [ ] 选桶时已写入 `bucket_name`、`egress_ip`、`inflight_before` 到 `ContextVar`
- [ ] `try/finally` 覆盖**全部**转发分支（成功、HTTPError、超时、网络、其他异常）
- [ ] `fail_category` 覆盖 §3 所有枚举，且 `http_4xx_other` 与 `http_429` 正确区分
- [ ] `upstream_status` 在异常时为 `null`，在 HTTP 响应时为真实状态码
- [ ] `latency_ms` 用 `perf_counter()` 计量
- [ ] `model` 仅安全提取顶层字段，不解析消息内容
- [ ] 日志不含 `Authorization`、API Key、消息体
- [ ] 容器内可写入 `/var/log/zen-gw/requests.jsonl`，宿主机卷已挂载
- [ ] `logrotate` 配置已生效（`logrotate -d /etc/logrotate.d/zen-gw-requests` 检查语法）
- [ ] 首次落地后用 `tail -f` 验证字段齐全（`bucket_name + egress_ip + outcome + fail_category`）