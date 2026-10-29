# 运维手册（OPERATIONS）

> 现状快照：2026-10-28（0.7.4，四桶现役）。本文只记**当前部署状态、常用操作、
> 排障路径**与未解决风险；事故编年与已关闭问题看 §5 与
> [`CHANGELOG.md`](../CHANGELOG.md)。拓扑/端口/池语义看
> [`ARCHITECTURE.md`](ARCHITECTURE.md)；协议事实与红线看
> [`AGENTS.md`](../AGENTS.md) 硬约束 1；模型参数看
> [`MODEL_PROFILE.md`](MODEL_PROFILE.md)；凭据去向看 [`ACCESS.md`](ACCESS.md)。

## 1. 现状（0.8.x 双容器）

```
客户端 → http://115.29.231.25:10002/v1    (明文直连 + API key；唯一入口)
      → 容器 zen-gw-edge:10002             (只透传 + 首字节前选路)
          ├ primary  → zen-gw:10002        (网关本体，compose 网络内)
          └ fallback → 172.17.0.1:10003    (proxy_ms / ModelScope
                                            deepseek-ai/DeepSeek-V4.1-Flash-1017)
      → egress 池按桶 CONNECT 到设备 agent（bun 承担上游 TLS/H2 一跳）
      → opencode.ai/zen/v1
```

> 2026-10-29 框架重构：网关必须频繁改（上游形状门），入口不能跟着重建。于是把
> 公网 `10002` 交给 `zen-gw-edge`，网关退到 compose 网络内部。重建网关时 edge 不
> 动，用户看到的是"切到备用模型"，不是连接错误。详见 §2.0。

> 2026-10-28 变更：取消 caddy TLS 中转，容器直接发布 `10002`。理由——客户端
> 装证书的代价大于收益（用户明确否决）；域名链路实测 23–68s，不可用。

> 域名入口 `llm.223722.xyz` **已彻底下线**（2026-10-29）：`100.90.84.65` 上的
> `cloudflared-tunnel` 与 `zen-lb` 已停止、disable 并删除 unit，`/etc/cloudflared`
> 与 `/root/.cloudflared` 已删。2026-10-25 它就已不可用（只剩 `http_status:404`
> 兜底规则）；本次是把无源的空壳一并清退。见 ARCHITECTURE §10。

对外只暴露一个掩码别名 `deepseek-ai/deepseek-v4.1-flash`（`PUBLIC_MODELS` 第三
列决定上游，当前 `big-pickle`）。旧栈（`9443` + `zen-lb` + 各设备完整网关）已于
2026-10-17 从 ECS 清退；设备侧 `:8791`/`:8793` 旧网关已于 **2026-10-29 删除**
（计划任务、部署树、日志、payload_dump 全清，见 CHANGELOG `Unreleased`）。

## 2. 部署与桶状态

### 2.0 双容器与"不中断重建"（0.8.0 起）

| 容器 | 端口 | 职责 | 会不会被频繁重建 |
|---|---|---|---|
| `zen-gw-edge` | 宿主 `10002` → 容器 `10002` | 公网唯一入口：透传 + 探针 + 首字节前改投备用 | 否（策略全在环境变量） |
| `zen-gw` | `expose 10002`（compose 网络内） | 网关本体：协议转换、出口池、进程内 fallback | 是（每次改上游形状都要） |

edge 的降级判据（全部在**响应首字节发给客户端之前**决定，所以切换对客户端不可见）：

| 触发 | 行为 |
|---|---|
| 探针连续失败（`GET zen-gw:10002/healthz` 每 2s，超时 2s） | 直接走备用，不再打主链 |
| 连接被拒 / 超时（重建中） | 同一请求内改投备用 |
| `502/503/504`（`EDGE_FAILOVER_STATUSES` 可改） | 同一请求内改投备用 |
| `429` 且 `Retry-After > 1h` 或 `code=free_quota_exhausted` | 改投备用（主链要等窗口重置） |
| `400/401/404/422` 等 4xx | **不**降级：这是答案不是故障 |

备用请求会做三件事，保证客户端无感：换 model 为
`EDGE_FALLBACK_MODEL`、换 `Authorization` 为 `EDGE_FALLBACK_API_KEY`、把响应里的
`model` 改回客户端请求的那个。响应头 `X-Edge-Route: primary|fallback` 与
`X-Edge-Fallback-Reason` 是唯一可见差异。

只有 `/v1/chat/completions` 可降级（备用中继只说这一种协议），`/v1/responses`、
`/v1/messages` 在主链挂掉时保持原错误，不会被静默换成另一种格式。

**日常只重建网关（推荐，用户仅看到切到备用模型）**：

```bash
cd /opt/zen-gw/src && docker build -q -f deploy/ecs/Dockerfile -t zen-gw:0.8 .
cd deploy/ecs && docker compose up -d --no-build zen-gw     # 只动这一个容器
```

edge 会在 2s 内把流量切到 `proxy_ms`，重建完探针恢复后自动切回。

**需要零感知（连模型 ID 都不换）时**：重建前手动 drain，完了切回 auto：

```bash
K=$(grep '^EDGE_ADMIN_KEY=' deploy/ecs/.env | cut -d= -f2-)
curl -s -H "X-Admin-Key: $K" -X POST -d '{"mode":"fallback"}' http://127.0.0.1:10002/edge/mode
# ... 重建 zen-gw ...
curl -s -H "X-Admin-Key: $K" -X POST -d '{"mode":"auto"}'     http://127.0.0.1:10002/edge/mode
curl -s -H "X-Admin-Key: $K" http://127.0.0.1:10002/edge/status   # 看 primary.alive
```

**验收**：`curl -s http://127.0.0.1:10002/healthz` 必须同时有 `role: edge`、
`primary.alive: true`、`fallback.configured: true`；三个缺一就说明 edge 的 env 没接对。

> **⚠️ 只重建网关时不要 `docker build -t zen-gw:0.8` 后再 `up -d`**
>
> compose 里两个服务都引用 `zen-gw:0.8`。把该 tag 指向新镜像后再 `up -d`，
> edge 会**一起被重建**（2026-10-29 实测：`Container zen-gw-edge Recreate`）——
> 它的 10002 端口会短暂不可用，等于把"改网关不影响用户"的收益丢掉。
>
> 只想换网关本体时（edge 代码没改），用 compose 的 `--no-deps`：
>
> ```bash
> docker build -f deploy/ecs/Dockerfile -t zen-gw:0.8 -t zen-gw:<ver> .
> cd deploy/ecs && docker compose up -d --no-build --no-deps zen-gw
> ```
>
> 两条都改了才需要整体 `up -d`（此时那几十秒的入口空窗由备用链之外的
> DNS/TCP 层承担，无法避免）。

**重建期间的可用性**：`zen-gw` 停着的时候，edge 会按探针失败直接改投
`172.17.0.1:10003`。2026-10-29 实测：`docker rm -f zen-gw` 之后立刻发请求，
客户端拿到 `200`，响应头 `x-edge-route: fallback` +
`x-edge-fallback-reason: ConnectError: ... name resolution`。这就是 0.8.0 拆分
入口的目的，**重建时不需要停 edge，也不需要通知用户**。


`/opt/zen-gw/src` = **仓库 HEAD 的 `git archive` 快照**，不再是手工维护的
「deploy 快照」。2026-10-28 已彻底重建：旧快照里的 `proxy_opencode.bak-2026*`、
`*.egg-info`、`.pytest_cache`/`.mypy_cache`/`.ruff_cache`、`runtime-logs`、
`.e2e_tmp`、`src-backup-*.tgz` 全部清除（2.8M，只有源码）。宿主 `10002 → edge 容器 10002`（明文直连；0.8.0 起 edge 独占该端口）。`/data` 属主必须
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
  cd /opt/zen-gw/src && docker build -f deploy/ecs/Dockerfile -t zen-gw:0.8 -t zen-gw:<ver> .
  cd deploy/ecs && docker compose -f compose.yml up -d --no-build
  rm -f /root/zen-src.tar.gz'

# 验收（三件缺一不可）
curl -s http://115.29.231.25:10002/healthz                # version 与 tag 一致
python scripts/e2e_workbuddy.py --base http://115.29.231.25:10002 --key <GW_KEY>
```

硬规矩：①`.env` 与 `data/` 是唯二不可再生项，先备份再动手；②镜像只保留
`zen-gw:0.8`（compose 引用）与当前版本 tag，回滚靠 git + 重建（约 40s），不囤
rollback 镜像；③上传包用完即删，部署树里不许出现 `__pycache__`/缓存/备份。

> **`chown -R 10002:10002 data` 不是可选项**（2026-10-29 又踩了一次）：容器以
> `USER gw`（uid 10002）运行，而部署是 root 在操作。root 重建/回填 `data/` 后
> 属主变成 0:0，`EventLog.emit` 拿到 `PermissionError` 就把 sink **永久禁用**
> （设计如此：绝不因 I/O 失败抛错打断请求），于是账本静默停写、`docker logs` 里
> 只剩一行 `egress event log disabled after write failure`。症状是「池子在动、
> 账本不涨」，归因链断掉。回填后务必 `ls -ln deploy/ecs/data` 确认属主是 10002，
> 并 `docker exec -u 10002 zen-gw sh -c 'echo x >> /data/egress_events.jsonl'`
> 试写一次（记得删掉那一行）。

| 桶 | agent | 最近出口 IP | 备注 |
|---|---|---|---|
| `owin10-mobile` | `100.109.109.108:10001`（有线 `bind_source 192.168.1.222`） | `36.149.55.x` 移动 | ONT V173-50 `kind=huawei`，入 `ZEN_EGRESS_ROTATION=30`，实弹成功 |
| `owin10-telecom` | `100.109.109.108:10002`（WiFi `bind_source 192.168.2.235`） | `180.117.x` / `121.234.x` 电信 | ONT HG8020C `kind=huawei_ct`，实弹成功；前缀含 `180.117.0.0/16` |
| `cc9` | `100.87.219.115:10001` | `116.147.x` / `122.195.x` 联通 | chroot agent + Magisk 看门狗；ONT HG6142A-C `kind=fenghuo` |
| `esp32-bucket` | ECS 本机适配器 `172.18.0.1:10005`（systemd `esp32-bucket-adapter.service`，HELLO 鉴权协议 v4） | MiFi 移动（`egress_ip` 由控制通道源地址实测，`egress_ip_source=control-peer`） | ESP32 反向隧道；**隧道空闲上限曾 20s（<网关 60s 首字节期限）导致慢请求全灭，0.7.9 修固件为 180s，需重烧**；3 连败自动 cordon；换 IP 用 `tools/reboot_mifi.py` |
| ~~`win10`~~ | — | — | 2026-10-25 移出池：与 owin10-mobile 同 CGNAT 出口、无独立换 IP 能力 |
| ~~`ecs` 本机桶~~ | — | — | 2026-10-17 移出：机房 IP 共享流量即耗 |

关键 env（`deploy/ecs/.env`，模板见 `env.example`）：

```
ZEN_EGRESS_PROXIES / ZEN_EGRESS_EXPECT_PREFIX / ZEN_EGRESS_ROTATION   # 三处桶清单必须同步
ZEN_EGRESS_TOKEN_ROTATION      # 主触发：每桶每窗口估算 prompt tokens 上限（生产 1.2M，见下）
ZEN_ROTATE_ON_HARD_429=true    # 硬 429 = IP 配额判决 → 立即换 IP（0.8.6 起生产开启）
ZEN_BUN_TRANSPORT=1            # 传输栈门：生产必须走 bun
ZEN_MARKER_MODE=full           # bridge 注记过不了 body 门
PUBLIC_MODELS=deepseek-ai/deepseek-v4.1-flash:DeepSeek V4.1 Flash:big-pickle
ZEN_EGRESS_ROTATE_MIN_INTERVAL_S=1800   # 硬 429 换 IP 下限，防轮换风暴
ZEN_TTFB_S=60                  # 响应头超时（只约束首字节，默认 60s）
ZEN_TTFB_DEMOTED_S=15          # 降权桶的响应头超时（只缩短，0=关闭分级）
ZEN_EGRESS_COOLDOWN_S=20       # 单次失败→降权秒数（×连续次数，不再移出轮换）
ZEN_EGRESS_COOLDOWN_MAX_S=300  # 降权上限＝旧 cordon 长度（传输失败永不 cordon）
ZEN_FALLBACK_BASE_URL / _API_KEY / _MODEL_MAP / _DEFAULT_MODEL   # 备用链路，见 §3
ZEN_FALLBACK_RECOVER_AFTER=2   # 主链路连续成功多少次才算恢复（防抖动）
```

**单桶故障的语义（0.8.5 起，用户明确要求）**：一个桶失败一次**不会**离开轮换。
该请求照旧交给备用链（proxy_ms 的 `DeepSeek-V4.1-Flash-1017`），这个桶只是排到
选择顺序末尾（`egress_cooldown`，20s × 连续次数，上限 300s），下一请求正常回到
轮询——包括它自己；成功一次立即恢复。**任何次数的传输失败都不 cordon**（第一版
保留「3 次失败 escalate」，被演练推翻：esp32 累积到 13 次仍被隔离，而隔离恰恰
是用户要禁掉的行为）。cordon 只留给 IP 域判决（硬 429 日配额、free-tier 403）。
与此配套，处于降权状态的桶只拿到 `ZEN_TTFB_DEMOTED_S` 的首字节期限，避免同一个
腿让用户连等两个满分钟。

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
4. **真实日配额**：只有这一项换 IP 可能有效，且极难遇到——见下面的更正。

> **同形 429 的头号成因是 body 门，不是额度（2026-10-29 03:00 CST 更正）**
>
> 02:00 那次「四个 IP + ECS 自身 IP + 重启 MiFi 的全新 IP 全部 429」被误判为
> 账号级额度耗尽（0.7.12–0.7.16 就是按这个结论改的）。真因用真实 CLI 抓包重放
> 定死了：同 IP、同 bun 传输栈，**CLI 原始请求 200×3，我方请求 429×3**。
> 两个具体差异：
> - 内置工具集**过期且被裁成子集**：1.19.5 的门要求当前客户端的全部 12 个内置
>   工具，只留 1 个（或旧的 4 个）→ 429；12 个 + 追加自定义工具 → 200。
> - **日期用了 UTC**：带桥接说明时 UTC 日期被判「非实时渲染」→ 429；换成
>   +8 日界（CST）日期 → 200×3。
>
> 所以排查顺序改为：**先用真实 CLI 在同 IP 重放同形请求**，CLI 能过就说明是我方
> body/headers（`ZEN_TOOLS_MODE=full`、prompt 资产版本、日期口径），与额度无关。
> 抓包方法见 `scripts_local/capture_server.py`（把 `provider.opencode.options.baseURL`
> 指向它即可拿到 CLI 的真实请求体）。
>
> 旧的「账号级额度」结论**不再作为依据**；`quota_blackout` 机制保留——真正的
> 额度耗尽确实存在，但它表现为**所有模型、所有 IP 同时 429**，且换成 CLI 也一样。

`ZEN_ROTATE_ON_HARD_429` **生产已开启（0.8.6）**：硬 429 = IP 配额判决 → 立即
换 IP。证据链（2026-10-29 定案）：09:15 实测同账号在 mobile 硬 429 时
telecom/cc9 继续 42 发全 200（额度按 IP 计）；cc9 实弹换 IP
（122.195.202.133 → 112.83.208.198）后立即恢复服务。防风暴靠
`ZEN_EGRESS_ROTATE_MIN_INTERVAL_S=1800`（同桶两次成功轮换下限 30 分钟）+
每窗口轮换次数上限 5（0.8.6 起，`MAX_ROTATIONS_PER_WINDOW`）。
`401 AuthError: Missing API key` = 模型门：请求了付费裸 ID（`deepseek-v4.1-flash`
这类没有 `-free`/`big-pickle` 的 id）。公开别名要靠 `PUBLIC_MODELS` 第三段或
`ZEN_MODEL_MAP` 映射到免费 id——0.7.17 起两者都生效，别名发布即映射。
`403 FreeTierError` = 形状门（内置工具名不全或 `stream:false`）+ 出口 IP 门槛
（防御路径）；网关按 `ZEN_TOOLS_MODE=full` 注入全部 12 个内置工具。

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

### 4.3.1 客户端 `Connection error.`（连不上，非断流）

这是**客户端侧**故障，特征是没到网关就失败——网关日志里**没有任何请求记录**。
判据是拿不到任何 HTTP 状态码，而不是拿到 429/500。

| 客户端报错 | 真实原因 | 一锤定音的判据 |
|---|---|---|
| `[SSL: WRONG_VERSION_NUMBER]` → `Connection error.` | 客户端写 `https://`，但 10002 是**明文 HTTP** | `curl -s http://<ip>:10002/healthz` 200，而 `curl -sk https://<ip>:10002/healthz` 返回 exit 35 / `appconnect=0` |
| `401 invalid_api_key` | 密钥不是 ECS `GATEWAY_API_KEYS` 的值 | 明文探测 200 但鉴权 401；比对密钥 sha256 而不是肉眼看长度 |
| `ConnectTimeout` / 无响应 | 安全组或 IP 变更，与网关无关 | `connect=` 时间超时（非秒级 RST） |

**根因教训（2026-10-29）**：10-28 取消 caddy TLS 后，`docs/` 里仍留有多处
`https://115.29.231.25:10002` 的旧写法，Hermes 照文档配置 → 全体请求
`WRONG_VERSION_NUMBER`。同时它的 `key_env` 指向一把 17 字符的旧密钥（ECS 唯一
密钥是 15 字符），两层故障叠加：**即使把 https 改成 http，下一跳仍会 401**。
排查时必须分别验证协议层与鉴权层，不能只修第一层就宣称修好。

诊断入口：Hermes 的 `logs/errors.log` 里搜 `WRONG_VERSION_NUMBER`（协议错）与
`invalid_api_key`（鉴权错）；网关容器日志**无记录**即证明请求没到达。

### 4.4 纪律

- 不要用真实用户密钥做批量探测；不要把请求正文、prompt、key 写入日志。
- 桶出口上**成功（200）**的活体调用会消耗该 IP 当日额度——实验少而精、
  clone-first；429/403/401/传输失败不烧额度。
- **上下文边界探测极贵**：顶到 26 万 token 的 200 响应一次就是几十万 prompt
  tokens（≈一个桶的日额度）。2026-10-29 的一轮扫描直接把 mobile 桶打到
  `free_quota_exhausted`，后续全部 429。要测就一次到位、别重复取样；
  需要重复取样时先算清楚预算。
- `httpx`/`httpcore` 的 INFO 日志会打印带凭据的 URL，`setup_logging` 已压到
  WARNING，不得回退。

### 4.5 上游版本漂移（每天都要能回答的问题）

免费层的形状门跟着 opencode CLI 的 release 走。**CLI 一发新版，门就可能开始回
配额形状的 429**，而指标上看起来只是"额度用完了"——2026-10-29 那次事故就是
1.19.5 的门被喂了 1.19.4 的工具集。

```bash
python scripts/sync_opencode.py           # 只报告；exit 3 = 漂移
python scripts/sync_opencode.py --write    # 刷新它能刷的部分（目前只有 prompt）
```

判据与自动化边界（别指望它全自动）：

| 项 | 来源 | 可自动化？ |
|---|---|---|
| CLI 版本 | npm `opencode-ai` 的 `latest` | ✅ 精确 |
| prompt 模板 | GitHub `session/prompt/default.txt` | ✅ 纯文本、可字节比对 |
| 内置工具**名字** | GitHub `tool/builtins.ts` 的模块清单 | ⚠️ 仅作提示（模块名 ≠ 线上名，且注册的比实际发的多） |
| 内置工具 **schema** | Effect `Schema`（TS） | ❌ 无法静态转换，必须真实 CLI 抓包 |

抓包姿势（`scripts_local/capture_server.py`，本机已验证）：

```bash
python scripts_local/capture_server.py 8899 captured.json      # 1
mkdir ~/ocprobe && cd ~/ocprobe
cat > opencode.json <<'EOF'
{"provider":{"opencode":{"options":{"baseURL":"http://127.0.0.1:8899/v1"}}}}
EOF
XDG_DATA_HOME=$PWD/xdg opencode run "reply with the single word OK" \
  --model opencode/big-pickle                                   # 2
```

第二条必须带 `XDG_DATA_HOME`：否则会用默认数据目录里的旧 DB，报
`Database is not empty and has no session table`。

> 1.19.5 抓包事实：`builtins.ts` 装了 12 个模块，实际只发 **11 个**工具名
> （`question`、`apply-patch` 注册但不提供给模型）。我方资产保留 12 个是超集
> ——门接受超集、拒绝子集，所以无需删。
>
> `.github/workflows/drift.yml` 每天 09:17 CST 跑一次；漂移即 CI fail。
> 它需要 `GITHUB_TOKEN`（匿名 API 只有 60 次/小时，共享 runner 秒光）。

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
