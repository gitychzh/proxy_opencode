# 运维手册（OPERATIONS）

> 现状快照：2026-10-28（0.7.4，四桶现役）。本文只记**当前部署状态、常用操作、
> 排障路径**与未解决风险；事故编年与已关闭问题看 §5 与
> [`CHANGELOG.md`](../CHANGELOG.md)。拓扑/端口/池语义看
> [`ARCHITECTURE.md`](ARCHITECTURE.md)；协议事实与红线看
> [`AGENTS.md`](../AGENTS.md) 硬约束 1；模型参数看
> [`MODEL_PROFILE.md`](MODEL_PROFILE.md)；凭据去向看 [`ACCESS.md`](ACCESS.md)。

## 1. 现状（0.7.x 单栈）

```
客户端 → http://115.29.231.25:10002/v1    (明文直连 + API key；唯一入口)
      → 容器 zen-gw:10002（直接监听公网，无 caddy、无证书）
      → egress 池按桶 CONNECT 到设备 agent（bun 承担上游 TLS/H2 一跳）
      → opencode.ai/zen/v1
```

> 2026-10-28 变更：取消 caddy TLS 中转，容器直接发布 `10002`。理由——客户端
> 装证书的代价大于收益（用户明确否决）；域名链路实测 23–68s，不可用。

> 域名入口 `llm.223722.xyz` 当前**不可用**（2026-10-25 实测 404：cloudflared
> 只剩 `http_status:404` 兜底规则，无 hostname ingress）。见 ARCHITECTURE §10。

对外只暴露一个掩码别名 `deepseek-ai/deepseek-v4.1-flash`（`PUBLIC_MODELS` 第三
列决定上游，当前 `big-pickle`）。旧栈（`9443` + `zen-lb` + 各设备完整网关）已于
2026-10-17 从 ECS 清退；设备侧 `:879x` 旧网关因入口断开而不可达闲置。

## 2. 部署与桶状态

`/opt/zen-gw/src` = **仓库 HEAD 的 `git archive` 快照**，不再是手工维护的
「deploy 快照」。2026-10-28 已彻底重建：旧快照里的 `proxy_opencode.bak-2026*`、
`*.egg-info`、`.pytest_cache`/`.mypy_cache`/`.ruff_cache`、`runtime-logs`、
`.e2e_tmp`、`src-backup-*.tgz` 全部清除（2.8M，只有源码）。宿主 `10002 → 容器 10002`（明文直连；2026-10-28 前是 caddy `:10002` 反代
loopback `10010`）。`/data` 属主必须
可写（uid 10002），否则 eventlog 静默禁用。

**重建流程**（改代码后照抄，端口/密钥/别名全程不动）：

```bash
# 本地：只发仓库里的东西，不带任何本地缓存
git archive --format=tar.gz -o /tmp/zen-src.tar.gz HEAD
scp -P 222 /tmp/zen-src.tar.gz root@115.29.231.25:/root/

# ECS：先备份两个不可再生的东西，再清空
ssh -p 222 root@115.29.231.25 'set -e
  cd /opt/zen-gw/src && cp deploy/ecs/.env /tmp/keep.env && cp -a deploy/ecs/data /tmp/keep.data
  docker rm -f zen-gw; rm -rf /opt/zen-gw/src; mkdir -p /opt/zen-gw/src
  tar xzf /root/zen-src.tar.gz -C /opt/zen-gw/src
  cp /tmp/keep.env /opt/zen-gw/src/deploy/ecs/.env          # 密钥/桶/别名原样
  cp -a /tmp/keep.data /opt/zen-gw/src/deploy/ecs/data
  chmod 600 /opt/zen-gw/src/deploy/ecs/.env
  chown -R 10002:10002 /opt/zen-gw/src/deploy/ecs/data
  cd /opt/zen-gw/src && docker build -f deploy/ecs/Dockerfile -t zen-gw:0.7 -t zen-gw:<ver> .
  cd deploy/ecs && docker compose -f compose.yml up -d --no-build
  rm -f /root/zen-src.tar.gz'

# 验收（三件缺一不可）
curl -s https://115.29.231.25:10002/healthz -k          # version 与 tag 一致
python scripts/e2e_workbuddy.py --base https://115.29.231.25:10002 --key <GW_KEY> --insecure
```

硬规矩：①`.env` 与 `data/` 是唯二不可再生项，先备份再动手；②镜像只保留
`zen-gw:0.7`（compose 引用）与当前版本 tag，回滚靠 git + 重建（约 40s），不囤
rollback 镜像；③上传包用完即删，部署树里不许出现 `__pycache__`/缓存/备份。

| 桶 | agent | 最近出口 IP | 备注 |
|---|---|---|---|
| `owin10-mobile` | `100.109.109.108:10001`（有线 `bind_source 192.168.1.222`） | `36.149.55.x` 移动 | ONT V173-50 `kind=huawei`，入 `ZEN_EGRESS_ROTATION=500`，实弹成功 |
| `owin10-telecom` | `100.109.109.108:10002`（WiFi `bind_source 192.168.2.235`） | `180.117.x` / `121.234.x` 电信 | ONT HG8020C `kind=huawei_ct`，实弹成功；前缀含 `180.117.0.0/16` |
| `cc9` | `100.87.219.115:10001` | `116.147.x` / `122.195.x` 联通 | chroot agent + Magisk 看门狗；ONT HG6142A-C `kind=fenghuo` |
| `esp32-bucket` | ECS 本机适配器 `172.18.0.1:10005`（systemd `esp32-bucket-adapter.service`，HELLO 鉴权协议 v4） | MiFi 移动（`egress_ip` 由控制通道源地址实测，`egress_ip_source=control-peer`） | ESP32 反向隧道；**隧道空闲上限曾 20s（<网关 60s 首字节期限）导致慢请求全灭，0.7.9 修固件为 180s，需重烧**；3 连败自动 cordon；换 IP 用 `tools/reboot_mifi.py` |
| ~~`win10`~~ | — | — | 2026-10-25 移出池：与 owin10-mobile 同 CGNAT 出口、无独立换 IP 能力 |
| ~~`ecs` 本机桶~~ | — | — | 2026-10-17 移出：机房 IP 共享流量即耗 |

关键 env（`deploy/ecs/.env`，模板见 `env.example`）：

```
ZEN_EGRESS_PROXIES / ZEN_EGRESS_EXPECT_PREFIX / ZEN_EGRESS_ROTATION   # 三处桶清单必须同步
ZEN_EGRESS_TOKEN_ROTATION      # 主触发：每桶每窗口估算 prompt tokens 上限（建议 8M）
ZEN_BUN_TRANSPORT=1            # 传输栈门：生产必须走 bun
ZEN_MARKER_MODE=full           # bridge 注记过不了 body 门
PUBLIC_MODELS=deepseek-ai/deepseek-v4.1-flash:DeepSeek V4.1 Flash:big-pickle
ZEN_EGRESS_ROTATE_MIN_INTERVAL_S=1800   # 硬 429 换 IP 下限，防轮换风暴
ZEN_TTFB_S=60                  # 响应头超时（只约束首字节，默认 60s）
ZEN_FALLBACK_BASE_URL / _API_KEY / _MODEL_MAP / _DEFAULT_MODEL   # 备用链路，见 §3
ZEN_FALLBACK_RECOVER_AFTER=2   # 主链路连续成功多少次才算恢复（防抖动）
```

**EXPECT_PREFIX 是审计字段，不是可用性开关**（2026-10-25 起）：IP 一变即解除
IP 域 cordon 并接受轮换结果，前缀不符只记 `prefix_mismatch` 事件 + WARNING。它
仍应保持与当日实际落段同步——名单长期漂移会让审计事件失去意义（线上
`owin10-telecom` 落在 `117.95.x`/`49.x`，名单只写了 `106.x`/`121.x`）。

### 2.1 备用链路运维（proxy_ms）

主链路出口级故障时自动切到同机 `proxy_ms`（宿主 `0.0.0.0:10003`，系统服务
`proxy-ms.service`），机制见 `ARCHITECTURE.md` §7。

- 容器经 `172.17.0.1:10003` 访问，**不要带 `/v1`**（适配器自己拼路径）。
- 状态：`curl -s http://127.0.0.1:10002/healthz` 看 `fallback.engaged` /
  `hold_remaining_s`；`docker logs zen-gw --since 1h 2>&1 | grep FALLBACK`。
- **事故级留痕（2026-10-27 起）**：首次跳闸 = ERROR
  `PRODUCTION INCIDENT - FALLBACK ENGAGED`，重复跳闸 = INFO `FALLBACK
  RE-ENGAGED`，恢复 = WARNING `PRODUCTION INCIDENT CLOSED - FALLBACK
  RECOVERED`；JSONL 事件带 `severity`（incident/continuation/recovered），
  可 `grep severity\": \"incident` 直接拉事故清单。
- **备用也会挂**：proxy_ms 欠费回 `429 insufficient balance`，网关原样透传并
  立即释放保持窗口，客户端会判 `billing/retryable:false` —— 那是备用链路的账，
  不要去查 zen 侧。
- 演练（不动生产容器）：起临时容器把主链路指到死 key（死端口也行），打一次
  请求即可看到 `PRODUCTION INCIDENT - FALLBACK ENGAGED` + 备用真实回包 +
  `x-gateway-fallback-*` 响应头；hold 过期后自然 RE-ENGAGE，完成后
  `docker rm -f zen-gw-probe`（2026-10-27 实弹演练通过）。

### 2.2 owin10 上 Hermes 客户端（只保留 `oc_uni`）

`openai-api` 是 Hermes **内置** provider，picker 永远列出；它"看起来被配上"是
因为 `auth.json` 的 `credential_pool` 会被 backing source 反复播种（
`OPENAI_API_KEY` / `gh auth token`），只删 JSON 行几分钟内复活。三步缺一不可：

1. `auth.json`：`providers`/`credential_pool` 置空，写 `suppressed_sources`
   （`openai-api: ["env:OPENAI_API_KEY"]`；`copilot: ["gh_cli","env:GH_TOKEN",
   "env:GITHUB_TOKEN"]`）。
2. `config.yaml`：`providers:` 只留 `oc_uni_id`，并加官方隐藏开关
   （必须是 YAML 真 list，字符串形式会被静默忽略）：
   ```yaml
   model_catalog:
     excluded_providers:
       - openai-api
       - copilot
   ```
3. 重启 Hermes（gateway 与桌面版都读配置；桌面版还会缓存
   `provider_models_cache.json`，必要时 `hermes model --refresh`）。

验证：`hermes config get model_catalog.excluded_providers` 回 list、
`hermes auth list` 无 pooled 条目。

## 3. 常用操作

```bash
# 日志（容器 stdout → json-file，8MB×3）
docker logs zen-gw --since 30m 2>&1 | grep -E "chat completion|stream stalled|failover|cordon"
docker compose -f /opt/zen-gw/src/deploy/ecs/compose.yml up -d --build   # 重建

# 桶状态 / 手动换 IP（管理员 key，仅 Tailscale/loopback）
curl -s http://127.0.0.1:10002/admin/egresses -H "Authorization: Bearer <key>"
curl -X POST http://127.0.0.1:10002/admin/egresses/<桶名>/rotate -H "Authorization: Bearer <key>"

# 结构化事件（换 IP 全时间线含前后 IP、cordon、IP 变化）
tail -f /opt/zen-gw/src/deploy/ecs/data/egress_events.jsonl
```

桶请求量统计口径与 jq/python 示例见 [`EGRESS_OBSERVABILITY.md`](EGRESS_OBSERVABILITY.md)
（必须带 `bucket_name + egress_ip`）。

质量门禁：`pytest tests/`、`ruff check proxy_opencode
tests scripts`、`mypy proxy_opencode`，以及 `egress-agent/` 侧同三项；
`python scripts/e2e_protocols.py` 用 mock upstream 做协议端到端。

## 4. 排障

### 4.1 断流 / 429 / 401 的归因顺序（硬性）

`429 FreeUsageLimitError` **四个互斥成因且响应完全同形**，只有最后一项能靠换
IP 解决。**不得仅凭 429 触发换 IP**（2026-10-25 曾因此单日换 IP 32/20/13 次，
把隧道打散）：

1. **传输栈**：必须 bun（`ZEN_BUN_TRANSPORT=1`，镜像内置）。httpx 同字节重放
   会被伪装配额 429。
2. **prompt 新鲜度**：`zen_prompt_default.txt` 只是模板，`_render_prompt()` 每
   请求重写模型名 / UTC 当天日期 / 工作区路径。冻结快照 = 同形 429。
3. **session 单次使用**：每次上游试探必须重新 mint（`chat()` in-loop）；复用会
   429。
4. **真实日配额**：额度是**账号级**（2026-10-29 实测，见下），换 IP **无效**。

> **额度口径更正（2026-10-29 02:00 CST）**：此前记为「per-IP 日配额、换 IP 有效」
> 是错的。实测同一分钟内，四个出口 IP、ECS 自己的 IP，以及刚重启 MiFi 换来的
> 全新 IP（`111.55.72.119` → `111.55.74.1`，SSID 已回 `YCHZH`）**全部**返回
> `FreeUsageLimitError`，`Retry-After` 一致指向次日 08:00 窗口重置；连换
> `deepseek-v4.1-flash-free` / `glm-5.3-flash-free` / `mimo-v2.6-flash-free`
> 这些**其它免费模型**也同为 429（`beam-free` 是 403，属另一类门）。
> 结论：
> - 免费层额度按**账号**计，`轮换 IP 只换限流桶，不换额度`；
> - 额度耗尽的唯一出路是**等窗口重置**（CST 08:00）或换付费链路；
> - 全池都带配额判定时，适配器直接返回 429（`quota_blackout`），不再逐桶
>   白跑上游；备用链路由 `FailoverAdapter` 按 `min(blackout, 24h)` 撑住熔断窗口。
> 因此**不要**为了「恢复额度」去轮换 IP——那是 2026-10-25 单日换 32 次 IP
> 打散隧道事故的同款浪费。

`401 AuthError: Missing API key` = 模型门：请求了付费裸 ID，改用免费 ID
（`-free` 或 `big-pickle`）。
`403 FreeTierError` = 形状门（<4 个真名内置工具或 `stream:false`）+ 出口 IP 门槛
（防御路径）；网关已自动注入 4 真名工具并 cordon/failover。

**慢请求（>60s）先分相，再谈归因**：`chat completion.latency_ms` 与同 request_id
的 `stream ended.duration_ms` 之差就是「首字节前」的耗时。

- 差值 ≈ 300s：上游跳首字节悬挂（旧隧道黑洞），属 `ZEN_TTFB_S` 覆盖范围；修复前
  会一直等满 `zen_timeout_s`（实测 305–315s，12h 内 5 次）。日志里看
  `egress failover: ... [TtfbTimeout]` / `before response headers`。
- 差值小、`duration_ms` 大：上游真在慢速生成，与网关无关。
- `stream ended.reason=stalled`：流中途静默超过 `STREAM_STALL_S`。

### 4.2 看桶

```bash
curl -s http://127.0.0.1:10002/admin/egresses -H "Authorization: Bearer <key>" | python3 -m json.tool
# healthy / cordoned / cordon_reason / egress_ip / counters.last_rotation_*
docker logs zen-gw 2>&1 | grep -E "bun upstream attempt|answering 503|FALLBACK"
```

- `cordon_reason=free_quota_exhausted`（绑到 UTC0）或 `free_tier_rejected`：换
  IP 后探针观测到合法新 IP 会自动 `cordon_lifted`；前缀不覆盖则解不开。
- 全部桶 `healthy=true` 但客户端报错：查 §4.1 的门，而不是桶健康。
- `answering 503` / 客户端收到 `egress_unavailable`：全池耗尽，等自动轮换或手动
  rotate。

### 4.3 流问题

| 现象 | 日志锚点 | 含义 |
|---|---|---|
| 客户端 `stream_drop`，上游静默 | `stream stalled` / `stream_interrupted`（带 `egress=`） | 网关→上游段故障；看门狗合成 `finish_reason:"length"` 收尾，该桶按失败累计 |
| 客户端 600s 零字节后断开 | `client write stalled` | 客户端段黑洞（背压阻塞），监视任务主动取消响应任务并释放上游 |
| 流被截断但上游正常 | `stream ended reason=cut` | 上游干净截断，按 ok 释放；属上游行为 |

> 紧随 `client write stalled` 之后的 uvicorn `ASGI callable returned without
> completing response` ERROR 是**预期**（我们主动取消响应任务以关连接）。

### 4.4 纪律

- 不要用真实用户密钥做批量探测；不要把请求正文、prompt、key 写入日志。
- 桶出口上**成功（200）**的活体调用会消耗该 IP 当日额度——实验少而精、
  clone-first；429/403/401/传输失败不烧额度。
- `httpx`/`httpcore` 的 INFO 日志会打印带凭据的 URL，`setup_logging` 已压到
  WARNING，不得回退。

## 5. 事故编年（压缩，详情见 CHANGELOG）

| 日期 | 事件 | 定性 |
|---|---|---|
| 10-13 | 免费层 session ID gate（ascending → 403） | 1.19.0 起必须 descending 编码；0.6.4 修 |
| 10-17 | 协议合规仍 403 | 出口 IP 被门槛标记 → cordon + 换 IP；后重解读为形状门主因 |
| 10-19 | hermes `stream_drop` ×2 | ①上游静默（`STREAM_STALL_S`）②客户端段黑洞（`CLIENT_STALL_S`）；同时修容器日志丢失 |
| 10-19 | 兜底耗尽透传 403 | 客户端判 auth 不可重试 → 改 503 `egress_unavailable` |
| 10-24 | 三桶连环 429 | cordon 不随换 IP 解除 + rotate POST 超时误判 + 僵尸桶 `ready=true`，三处修复 |
| 10-24 | 全池 429 三次归因 | 探针烧穿 → CGNAT 新 IP 也被烧 → **终局：传输栈门**（必须 bun） |
| 10-24 | session 复用 429 | 上游 session 单次使用 → 每次试探重新 mint |
| 10-25 | body 门 | prompt 必须现场渲染（模型/日期/工作区）；冻结快照 = 同形 429 |
| 10-25 | 换 IP 风暴 | 32/20/13 次轮换源于把非配额 429 当配额 → 加 30 分钟下限与 429 归因红线 |
| 10-25 | 备用链路 | proxy_ms 欠费透传 → 备用返回错误即释放熔断窗口 |
| 10-25/26 | 305s 慢请求 + 日志噪音 | 首字节无超时（300s 唯一上界）、探针异常未判不健康、cordon 非幂等、`expect_prefix` 挡住 cordon 解除、熔断无迟滞 —— 五项同时修复，见 CHANGELOG |

## 6. 风险与待办

1. **`egress_request` per-call 账本缺口**：当前构建不写逐请求行，只有
   cordon/rotation/fallback 类事件入流（`ZEN_EGRESS_EVENT_LOG`）。按桶统计请求量
   暂无数据源，设计稿见 `EGRESS_OBSERVABILITY.md`。
2. **同出口多桶分摊额度**：`owin10-mobile` 与任何同 CGNAT 出口的设备共享上游
   额度；同出口桶越多，单 IP 额度越快烧穿。
3. **容器时区**：容器 UTC（计数窗口按 Asia/Shanghai 计算，不受影响）；对账需
   加 `TZ` 并重建。
4. **公网入口域名化**：当前只剩 IP 直连（需分发/更新自签 CA）。域名入口
   `llm.223722.xyz` 实测 404（cloudflared 无 hostname ingress），修复方案见
   ARCHITECTURE §10；大陆节点走域名还需 ICP 备案。
5. **`/healthz` 暴露桶拓扑与错误摘要**：应评估收至管理员接口或脱敏。
6. **上游偶发 400**：`stream=false, tools=false` 形态（2026-10-25 起 24h 内
   1 次），错误体原样透传；三种常见请求体复现均 200，未定位触发字段。下次
   出现时带 `x-request-id` 对比上游日志。
7. **`ZEN_TOOLS_MODE=captured2` 与 AGENTS 默认 `minimal` 不一致**：线上显式
   设置且功能正常，属文档与线上配置漂移，收敛前以 AGENTS 为准。

## 7. 历史：网络事件与旧栈（仅防误读）

- **移动 ↔ CF Anycast 丢包（10-02/03）**：故障域是移动网到特定 CF IP
  （`172.67.156.243`）的对等路由；电信命中同一 IP 正常。缓解：IPv4 优先、
  MTU 1480、经 Tailscale 直连 ECS。
- **直连入口被拦（10-03）**：杭州 ECS 受阿里云 ICP 合规拦截，按 `Host` 头 /
  TLS `SNI` 的**域名**生效且不限端口；`223722.xyz` 未备案 → 全端口被封。故
  客户端连 **IP 字面量**（不发 SNI）绕过；`llm.223722.xyz` 能用是因为 CF 在边缘
  终止 TLS、cloudflared 主动出站。
- **旧栈端口/服务**：`9443`、`zen-lb:7892`、`zen-glm53`、`cloudflared-ingress`
  均已停止并删除；历史文档里的 `llm2.223722.xyz` 计划命令**不是**当前步骤。
- **opc2**：设备已永久下线；其 uid 策略路由、ONT cron 等记录见
  `egress-agent/deploy/README.md` 与 `ARCHITECTURE.md` §10。
