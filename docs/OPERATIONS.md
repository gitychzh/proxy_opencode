# 运维手册（当前事实）

> 现状快照：2026-10-25。本文只记录当前部署状态、操作方法和仍未解决的风险
> （§0–§0.5 为按时间序的事故/定案记录，§5 为历史网络事件，现状只看 §1/§2/§3/§6）。
> 版本历史与已关闭问题看 `CHANGELOG.md`；设备地址和登录方式看 `ACCESS.md`；
> 协议事实与安全红线看 `AGENTS.md`；拓扑与端口看 `docs/ARCHITECTURE.md`。
> 历史实验文档已合并或删除，避免将旧拓扑当作现状。
>
> **单栈**：旧栈（`llm.223722.xyz` + `9443` + zen-lb + 四桶完整网关）已于
> 2026-10-17 从 ECS 彻底清退（见 §1 末尾注记）；现役只有 0.7.x 新栈——网关在
> ECS Docker 容器、桶设备只跑 `egress-agent` 出口代理，客户端入口
> `https://115.29.231.25:10002/v1`，权威文档是 `docs/ARCHITECTURE.md`。

## 0. 2026-10-13 事故：免费层 session ID gate（已解决）

opencode 1.19.0（2026-10-11 发布）把客户端 session ID 的生成从 ascending 改为
`descending()`（`packages/schema/src/session-id.ts`）；Zen 免费层随 inference
服务切换新增 gate：`x-opencode-session` 必须为 descending 编码
（`~((ms<<12)+counter) & 0xFFFFFFFFFFFF`）且同毫秒 counter < 64，否则
403 `FreeTierError` "OpenCode's free tier can only be used from within
OpenCode"。0.6.3 及之前的网关恒发 ascending ID → 四桶全 403，hermes 整链不可用。

**修复（0.6.4）**：`zen_direct.opencode_id()` 恢复 `descending` 参数语义，
session ID 按 descending 生成（counter=1），`x-opencode-request`（msg_）维持
ascending。已部署 win10-local + 四桶，经公网 LB 7/7 实测 200。

**定位手法**（复现同类问题时照做）：用真客户端二进制
（`npm i -g opencode-ai@latest`，本机临时目录 + XDG_* 环境变量隔离）跑一次确认
免费层本身没死；把客户端 catalog 缓存 `models.json` 的 provider `api` 指向本地
回显服务器抓真请求；从捕获请求逐项消融。结论：只有 session ID 编码 + counter
参与判定（UA 版本 / client 名 / project 值 / `x-session-id` / marker 均无关），
counter 阈值实测为 63/64。诊断脚本存放于本机
`%TEMP%\opencode\ablate*.py`（未入库，仅一次性取证）。

## 0.1 2026-10-17：免费层 403 的第二个根因 = 出口 IP 被门槛标记

现象：协议已合规（≥2 内置工具 + descending session + `stream: true`）的请求
仍持续 403 `FreeTierError`。

**定位（ECS 宿主直连做对照）**：同一份字节请求，经设备出口 IP 发出 403，
从 ECS 宿主 IP 直连发出 200；经设备 CONNECT 隧道与直连拿到同一张 opencode
真证书（无 MITM）、httpbin 回显无任何头注入 —— **唯一变量是源 IP**。**旋转换
到一个新 IP 后立即 200**（实测 cc9 `116.147.31.55` 403 → `122.195.202.2`
200；opc2 两腿同理）。即门槛按**出口 IP** 判定，被用过的 IP 会被标记；
而 `/healthz` 探针只看 agent 就绪与上报 IP，**不检测门槛**，所以被标记的桶
仍显示 healthy、继续被分发，用户端持续报错。

**修复（出口级失败处理）**：
- `zen_direct._exchange`：匿名模式 403 `FreeTierError` → 释放租赁为
  `free_tier_rejected`（失败），并让 `chat()` 桶间 failover（拒绝体在
  SSE 开播前，客户端零字节）；非匿名/其它 403 仍按请求级 ok 释放。
- `pool.release`：`free_tier_rejected` **立即 cordon** 该桶（不等 3 连败）。
- `rotate.on_release`：按 `counters.should_rotate_on_free_tier` **绕过阈值
  立即换 IP**，每窗口每桶至多一次，失败按 `ROTATION_RETRY_COOLDOWN_S` 冷却后
  可重试（与硬 429 共用去重/冷却）。
- 排障：整池 403 时逐个 agent `GET /healthz` 看出口 IP，必要时
  `POST http://127.0.0.1:10010/admin/egresses/{name}/rotate`（管理员 key，
  `10010` 是容器宿主映射，`10002` 是 caddy）手动换 IP。

## 0.2 2026-10-19：hermes `stream_drop` = 出口隧道静默（网关→上游段）

现象：hermes 桌面端报 `stream_drop`（"The connection dropped before the reply
finished."，model `deepseek-ai/deepseek-v4.1-flash`）；`logs/agent.log` 同刻有
`Clean EOF, no finish_reason`。（错误详情里的 `time:` 是客户端复制时刻，不是
故障时刻。）

**定位（故障在网关→上游段，hermes↔网关与网关进程本身均健康）**：
- zen-gw 容器连续运行 40h 无重启 → 非网关崩溃。
- `docker logs zen-gw` 四次 `httpx.ReadTimeout`（`stream relay failed`，栈
  `relay_stream → sse_mask → StreamRelay._stream → resp.aiter_bytes()`，2026-10-19
  03:13/03:22/03:28/03:34Z），与 opc2 `journalctl -u 'egress-agent*'` 中
  **opc2-wifi 隧道 open→closed 的生命周期逐一对应**（隧道建立后零字节，直到
  网关 300s 读超时才关闭）；opc2-eth 腿同时段正常轮转。
- `data/egress_events.jsonl` 当日已有两次 `egress_recovered opc2-wifi`
  （03:02/03:07，对应网关 `egress failover: upstream transport`）→ 该桶早前
  已在抖动。
- 同一时刻 hermes 11:22:43（UTC+8）= 网关 03:22:43Z，即 300s 读超时掐断连接的
  同一事件：异常穿透 `StreamRelay` → uvicorn 关闭客户端 SSE → hermes 报
  stream_drop。

**修复（三处，见 CHANGELOG 同日条目）**：
- `StreamRelay` 停顿看门狗 `STREAM_STALL_S`（默认 60s，覆盖响应头后首字节与
  字节间隔）：静默/中途传输异常 → WARNING（`reason=stream_stalled` /
  `stream_interrupted`）→ 走既有合成终止块路径收尾（`finish_reason:"length"` +
  `[DONE]`），客户端不再看到裸断连。
- 租约按结局释放：关闭钩子携带结局，`stalled`/`interrupted` 按**失败**
  （`kind=stream_stalled`/`stream_interrupted`）释放 → 3 连败 cordon 死桶
  （此前 `_released` 恒 `ok=True`，坏桶永不累计失败、永不 cordon）。
- 容器日志：`create_app_from_env` 先调 `setup_logging()`——Dockerfile CMD 走
  `uvicorn --factory` 而非 `python -m proxy_opencode`，此前容器根 logger 无
  handler：全部 INFO（`chat completion` / `stream ended`）被丢弃、WARNING 走
  lastResort 无格式无 extras，**无法从日志归因到桶**（本次定位因此被迫翻
  journalctl）。

**排障入口**：
```bash
docker logs zen-gw 2>&1 | grep -E "stream stalled|stream failed mid-way|stream ended"
# 新标记带 reason=stream_stalled/stream_interrupted 与 egress=桶名；
# 坏桶状态：curl -s http://127.0.0.1:10010/admin/egresses -H "Authorization: Bearer $ADMIN_KEY"
# 设备侧隧道生命周期：opc2 上 journalctl -u 'egress-agent*' --since "30 min ago"
```
**生效**：已随 2026-10-19 镜像重建部署生效（重建命令：`docker compose -f deploy/ecs/compose.yml up -d --build`；同批还含 §0.3 的 503 映射与容器日志修复）。

## 0.3 2026-10-19：兜底耗尽透传 403 被客户端判为 auth 错（已改 503）

现象：hermes 报 `HTTP 403: OpenCode's free models aren't available ... abusive
traffic from this IP address`（layer=auth、retryable=false），同一复制窗口
`POST /v1/chat/completions` 在网关确有一次 403 出口。

**定位（分类器无 bug，是兜底耗尽后的客户端呈现错误）**：06:39Z 单个请求烧穿
全部 3 次尝试——

```
06:39:20.684 cordon cc9（free_tier_rejected）→ failover http_403_free_tier（余 2 次）
06:39:20.769 failover transport（opc2-wifi 隧道死，余 1 次）
06:39:23.798 cordon opc2-eth（free_tier_rejected）→ 尝试耗尽 → 403 透传给 hermes
```

两次 403 均被 `_is_free_tier_rejection` 正确识别并 cordon/轮换，但 `attempts<=0`
的兜底把**出口级失败**原样透传，客户端按**请求级 auth 拒绝**理解（不可重试）；
而轮换约 90s 后全池已恢复（06:44 后复测 200）——本可重试成功。

**修复**：匿名模式下**最终** 403 `FreeTierError`（兜底耗尽或无池直连）不再透传，
改回 **503 + `error.code=egress_unavailable`**（`zen_direct._egress_gate_answer`，
同步记 WARNING `answering 503`）；非门槛 403 与带 key 模式仍按请求级原样透传。

**排障入口**：
```bash
docker logs zen-gw 2>&1 | grep -E "answering 503|egress failover|cordoned"
# 客户端收到 503 egress_unavailable = 全部桶门槛标记/隧道失败、兜底耗尽；
# 等自动轮换，或 POST /admin/egresses/{name}/rotate（管理员 key）手动换 IP。
```
**生效**：已随 2026-10-19 镜像重建部署生效（与 §0.2 同批）。

## 0.4 2026-10-19：hermes `stream_drop` = 客户端段 600s 黑洞（网关无过错）

现象：hermes 报 `stream_drop`（`deepseek-ai/deepseek-v4.1-flash`），错误里的
`time:` 仍是复制时刻；本次底层故障时刻是 **12:04:59Z**（UTC+8 20:04:59）
`Stream stale for 600s → WinError 10053`，35s 后重试成功。同一窗口早前还有
09:21/10:27/10:44Z 的 `Clean EOF, no finish_reason`。

**定位（故障在 hermes↔网关的最后一跳，网关/进程/ECS 全部干净）**：
- 与 §0.2（上游段静默）不同：本次网关侧上游**仍在正常产块**，`relay_stream`
  已中继 242 块并卡在 `send()`（uvicorn 背压 `flow.drain()`）——上游停顿看门狗
  （§0.2）看不到，TCP 也未断连，因此既无 WARNING 也无 `stream ended`，直到客户
  端自己的 600s 杀流。
- 网关 `stream ended reason=client_disconnected`（10 次）与 hermes
  `Clean EOF, no finish_reason`、caddy `context canceled`/`aborting` 逐秒对齐；
  该客户端段故障是**长期慢性**的（caddy `aborting` 计数：10-15 约 6、10-17
  约 88、10-18 约 151、10-19 约 26），非本次改动的回归。
- 已排除：Clash Verge（hermes 的 python 进程直连，不走 7897 系统代理）、ECS 网卡
  （0 error/drop）、本机 Wi-Fi 重连（24h 内无）、HTTP/2（全程 HTTP/1.1）、caddy
  对 SSE 压缩（`encode gzip` 默认 MIME 白名单不含 `text/event-stream`）。

**修复（客户端段看门狗）**：`relay_stream` 新增 `CLIENT_STALL_S`（默认 60s，
0 关闭）。语义：生成器停在 `yield` 恰等于 ASGI 在写这一块给客户端时被背压阻塞，
故"交出块后超过 `CLIENT_STALL_S` 没来取下一块"= 客户端段死；仅在上游读期间不清
零、故慢上游不会误报。触发时监视任务记 WARNING `client write stalled`、结束理由
标 `client_stalled`、取消响应任务（uvicorn 随即 `transport.close()`，客户端秒级
失败并可重试）、释放上游并写终态 `stream ended`。**注意**：监视任务自行完成上
游释放与终结日志——被取消的响应任务会经其 `CancelledError` traceback 钉住生成
器（`owner` 闭包成环），生成器 close 只能等环状 GC，若依赖它会拖慢释放与日志。

**排障入口**：
```bash
docker logs zen-gw 2>&1 | grep -E "client write stalled|stream ended"
# client write stalled 带 request_id/model/client/chunks/blocked_s；
# 流量增长中若 client write stalled 显著增加，说明公网段持续劣化（非网关 bug）；
# 紧随其后 uvicorn 的 "ASGI callable returned without completing response" ERROR
# 是预期行为（我们主动取消响应任务让它关连接），不是故障。
```

**生效**：`CLIENT_STALL_S` 随本轮镜像重建生效（0.7.x，2026-10-19 15:41Z 实测：
容器 healthy，`client_stall_s=60` 生效；6 路并发流全部 200 + `[DONE]`，
`stream ended reason=completed`，`client write stalled` 计数 0——默认阈值未误报）。

## 0.5 2026-10-24：三桶连环 429 兜底透传（billing 层）+ cordon 不随换 IP 解除

现象：hermes 01:54:01Z 报 `HTTP 429: Free usage limit reached. Subscribe to
OpenCode Go`（provider=oc1id → `deepseek-ai/deepseek-v4.1-flash`，layer=billing、
retryable=false）。`/v1/chat/completions` 直连转发上游匿名 429
`FreeUsageLimitError`（按出口 IP / UTC 日计量）。

**时间线（事件日志 `/data/egress_events.jsonl`，UTC）**：
- 01:42:31 opc2-eth 硬 429 触发轮换；`POST /rotate` 30s 超时
  `agent unreachable`（隧道抖动），`rotation_failed`——但 ONT 实际已在重拨，
  01:44:51 观测到新 IP `36.149.55.24`（合法前缀内、探针就绪）。
- 01:42:40–43:56 opc2-wifi 出口传输连败（16 连）cordon，01:49:23 恢复。
- 01:43:58 opc2-eth 免费额度 cordon：`free_quota_exhausted`
  `retry_after_s=80162`（烧穿的 IP 是 `223.68.27.82`）——cordon 绑到窗口末
  （UTC0），**尽管出口已换、新 IP 健康**。
- 01:43:59 cc9 硬 429 轮换成功 `116.147.31.217 → 112.83.208.62`；
  该 IP（联通 CGNAT 共享池）随后同样 429 —— 01:54:01 三桶皆不可用（eth 绑
  窗口、wifi 抖动中、cc9 新 IP 亦 429），兜底把 429 原样透传给 hermes。

**定位（两个根因级缺口，2026-10-24 修复）**：
1. 配额/门槛 cordon 是**按出口 IP 的判决**，但探针观测到 IP 变化时只记
   `egress_ip_observed` 不重估 cordon → ISP 侧自行重拨（非轮换编排路径）得到
   的健康新 IP 被旧判决压到 UTC0，池被减员，加剧兜底透传概率。
2. `POST {agent}/rotate` 传输失败即判 `rotation_failed`，**不再轮询** —— 命令
   可能实际已送达（本次 ONT 确实重启并换了 IP），失败记录颠倒因果。
   （opc2-wifi 的 rotate 于 2026-10-18 起多次因同因 refused。）

**修复（随本轮镜像重建生效）**：
- `egress.pool._probe_one`：观测到出口 IP 变化、新 IP 在 `expect_prefix` 内且
  探针 ready → `free_quota_exhausted` / `free_tier_rejected` cordon 立即解除
  （事件 `cordon_lifted`，failure 计数清零）。外前缀 IP（eth 腿回落 wifi）、
  探针未就绪、transport streak cordon 不解除；新 IP 若同样烧穿会在首个请求上
  重新 cordon。
- `egress.rotate._rotate_flow`：rotate POST 传输失败后照常轮询到 deadline，
  观测到合法新 IP 即按成功收敛（detail 注明 agent call 不可达）；明确 HTTP
  拒绝仍为最终失败（detail 同时带 transport 与 poll 两个原因）。

**追加定位（同日第二轮修复，健康面第三缺口——僵尸桶）**：
- 01:54Z 后复测发现：opc2-wifi agent `/healthz` 的 `ready` 是**硬编码
  true**——出口全灭（`bind_source` DHCP 漂移 `192.168.2.138`→`.235` 使
  隧道 socket 绑定失败，电信腿对 `opencode.ai:443` 自身亦不可达，POST 8s
  超时连败数小时）期间，agent 照答 ready=true，池以 ready 单一判据持续
  把客户端工作派给僵尸桶（502 燃料；见首轮修复里 01:49 恢复后的连败流）。
- 修复分两层：agent `health.py` `ready = egress_probe_ok is not False and
  探针新鲜`（每次探测尝试都打单调点 `probe_at`，`PROBE_READY_GRACE_S=45s`
  内算新鲜；环停转或最近一次探测失败 → ready=false）；网关 `pool.
  _probe_one` 增加 `egress_probe_ok is False → unhealthy` 的第二道防线
  （旧 agent 不带字段行为不变）。设备热修与代码无关但同窗完成：
  `/etc/egress-agent/wifi.json` `bind_source` 修正 `.235`
  （`640 root:opc2_uname`），agent 重启后绑定成功；
  当刻电信腿对 `opencode.ai:443` 仍超时（ISP 侧劣化，代码不可修，
  待其自愈/重拨恢复）。

**排障入口**：
```bash
docker exec zen-gw sh -c "grep cordon_lifted /data/egress_events.jsonl | tail -5"
# 免费额度 429 的当日分布（各桶烧穿时刻）看 eventlog 的 cordon 事件；
# 请求级明细需开 ZEN_EGRESS_REQUEST_LOG（见 docs/EGRESS_OBSERVABILITY.md）。
```

**注**：hermes 侧 `fallback_providers` 只配了 `oc1id`（同池），整池 billing
失败时无处可去；如需双上游兜底，可在 hermes 配置第二个 provider
（如 `proxykimi`），属客户端配置，不属本仓库。

**部署与生产验证（同日，两轮修复均已上线并实测）**：
- 02:39:17Z / 03:01:49Z `cordon_lifted` 两次在生产触发（opc2-eth
  `24`→`.217`、`.217`→`.125`，均 in-prefix + 探针就绪）——配额 cordon 随
  合法重拨 IP 解除的机制运转正常。
- 03:04Z 冒烟链实测：eth 新 IP `.125` 首个真请求即 429 —— CGNAT 共享池的
  "新 IP"可能**到手里时已被同池租户烧穿**；cordon-lift 只保证旧判决不
  迁移，首个请求仍会重判并立即重新 cordon（行为符合 ADR，勿把"换 IP"
  等同于"有额度"）。三桶全部 quota cordon 至窗口末（reset 2026-10-25
  08:00 CST），期间网关的正确表现为透明 429 + 及时派发 cordoned 桶。
- cc9 手动 admin 轮换（03:06Z）整 420s IP 未变——联通侧红拨当天拿不回
  新地址，属环境项。opc2-wifi 腿对 `opencode.ai:443` TCP 可通但 TLS
  失败（电信侧劣化，`ready=false` 正确压制）。**等待 08:00 窗口重置前
  无代码可为**；hermes 在窗口重度自愈重试（15x）会持续放大上游调用量，
  建议期间暂停或降频。

**第四桶 `win10`（Windows 本机，同日部署）**：
- 范围收敛：**网关不做任何改动**——本机只跑 egress-agent（CONNECT 隧道
  + `/healthz`，即"桶"本体），编排全在 ECS。agent 配置
  `C:\Users\<user>\.egress-agent\win10.json`（凭据=共享 agent secret，
  位置见 `docs/ACCESS.md`）；listener 只绑 Tailscale IP
  `100.121.137.118:10001`；无 ONT、不入 `ZEN_EGRESS_ROTATION`。
  启动：`deploy/windows/start-egress-agent.ps1`（幂等防重、healthz 自检）；
  用户 Startup 目录已放入开机自启 launcher。
- 接入：ECS `.env` 三处（PROXIES/EXPECT_PREFIX/不加 ROTATION）+
  `docker compose up -d`。池四桶全绿；**CONNECT 数据面真测通过**
  （经本机隧道对 opencode.ai 200）。本机与 opc2-wifi 同一家庭 WAN（同一
  公网 IP），给一条独立 ECS↔本机的传输路径，但**共享该 IP 的上游额度**。
  wifi 桶 expect_prefix 补 `106.111.0.0/16`（今晨重拨 IP 曾被旧前缀挡外）。
- **当日配额语义实测（重要）**：03:30–03:35 轮换连出**全新 IP**
  （联通 `122.195.202.20`→`.60`、移动 `.225`），全部**即刻 429** —— 与
  2026-10-17 的"新 IP=新额度"实测显著变严：当日窗口内靠换 IP 已**不能**
  恢复服务，整窗预期持续 transparent 429 直到 UTC0 重置（2026-10-25
  08:00 CST）。窗口重置后再复测一次确认额度模型（若仍即烧，需上报为
  协议层变化并更新 AGENTS 硬约束 1 的表述）。

**429 归因深挖（04:00–04:05Z，绕网关 cordon 的逐桶直刺实验）**：
- 基线：容器内复刻网关原样请求（同 header 序列/UA/`Bearer public`/
  内置工具/stream/descending session），经各桶 CONNECT 与 ECS 宿主直连
  —— 4 个互不相干 IP 族（电信家宽 win10/wifi、移动 eth、联通 cc9、
  阿里云机房 ECS）**全部 429**；响应仅 `Retry-After`（精确指向 00:00Z），
  无任何 `X-RateLimit-*`/scope 头。
- **用量轴排除**：`/data/egress_counts.json` 持久计数整窗 `count=0`
  （今 UTC 日**没有任何一个**成功上游 completion），首个 429 在
  00:59:32Z（窗口开跑仅 59 分钟）—— **"我们自己烧穿 500"不成立且
  从未接近**（500 是网关自己的换 IP 阈值，非上游额度；上游免费额度
  ≪500，且 429 与我们的用量、甚至与"该 IP 今日是否被我们用过"无关）。
- **变量消去逐项 429 依旧**：全新 `x-session-id` UUID（P1）、UA 换
  版本（P2）、去掉 `Authorization: Bearer public`（P4）、去掉
  `x-opencode-client/project`（P6）—— 全部仍然是 429。唯二穿层者：
  无效 model 直接 400 `Model is unavailable`（P3/P5）—— 证明限流层在
  模型解析**之后**，与我们 fingerprint/UA/token/session 无关。
- **结论（当日形态，**已被同日晚间消融推翻**——见下「破案与修复」）**：当时
  判为"上游在客户端不可改动维度计数"；晚间字节级克隆消融证明该结论错误，
  真差异在**工具名门槛**（形状门）与**配额窗口**（各 IP 当日额度早被探针
  消耗）两层，且形状修复后同出口/同栈同造型请求可 200。本段仅存档。

**破案与修复（06:15–07:20Z，决定性对照与消融阶梯）**：
- **对照组建立**：本机跑官方 opencode v1.19.4 客户端（npmmirror
  `opencode-windows-x64` 平台包，全新隔离目录、匿名、无 key）经 win10
  家宽出口**成功拿到 200 "ok"**；而同出口/当时的**字节级克隆**（本地
  HTTP 抓包服务器 `OPENCODE_CONFIG_CONTENT` 钓真客户端请求→原样重放，
  python httpx / bun 1.4.2/1.4.4 / curl 三栈、含 fork 2.0.18 形状与
  1.19.4 形状两组包）在**形状错误的包组**上稳定 429/403。
- **决定性单变量**：1.19.4 官方形状包重放 → **200**（SSE 5 条）；换成
  fork 2.0.18 抓包包（`x-session-id`/`x-session-affinity` 移名称 + 无
  `max_tokens/temperature/reasoning_effort` 键 + `shell/question/subagent`
  工具名集）→ 403/429。进一步消融锁定**唯一判定变量=tools 名集与数量**：
  - 12 名真名真 schema → 200；4 名真名（bash/edit/glob/read）+ **空
    schema/空 description** → 200；3/2/1 名真名 → 403；12 名中掺 1 个
    改名（`tool_0`）→ 403；2 名合成 schema（旧-minimal）→ 403。
  - **UA 完全不敏感**（换成 `opencode/latest/2.0.18/desktop` 同样 200）；
    schema/description 内容不校验；`x-session-id` 类头名重命名不敏感
    （1.19.4 名 ↔ 2.0 名互换 + b3/traceparent 加减均 200）。
- **两层门定案（修正前文与 AGENTS 硬约束 1 的旧口径）**：
  1. **形状门**（403 `FreeTierError`）= **≥4 个真名 opencode 工具**；
  2. **配额门**（429 `FreeUsageLimitError`）= **per 出口 IP / UTC 日**
     计数（`Retry-After` 精确指向 UTC0；实测 win10 家宽十几发成功调用
     后烧穿，retry 值 62799s）；形状门先于配额门判定（相邻两请求分别
     403/429 实证）。
  3. **当日全池 429 的真正成因就是第 2 层**：白天数百发排障探针本身
     即耗尽各桶 IP 的当日匿名额度（`egress_request` 账本 count=0 是因为
     账本不含探针，非上游无计数）；官方客户端"能用"与我们"不能用"的
     差异 = 形状门 + 各自残余额度，**不是 fingerprint/TLS/mihomo/IP 变换**。
- **修复（随本轮已提交）**：`zen_direct` `minimal` 注入 2→4（bash/edit/
  glob/read）、`captured2` 扩四具、UA 默认 `1.19.4/bun 1.4.4`、头集
  对齐 1.19.4 抓包（新增 `x-opencode-session-id` 双头、去 `x-session-id`
  与重复 `x-opencode-request-id`、`x-opencode-project` 固定 40-hex）；
  403 failover/cordon 语义不变。门禁全绿（pytest 258+33 / ruff / mypy）。
  **待 UTC0 重置后部署验证**（P7：新窗口首请求应 200）。

**第三轮定案（07:20–08:05Z，传输栈门——对上文"配额门"单因解释的最终修正）**：
- P7 验证时"新窗口 + 全新 IP 仍 429、而官方客户端/克隆探针 200"的矛盾在
  双栈对照下闭环（**同机、同出口 IP、同一鲜净额度窗口，分钟内**）：
  - bun 1.4.2（boringssl/HTTP2）重放克隆包 → **200**（4/4，含 fresh-
    session 每次新生）；
  - python httpx 0.28（OpenSSL 3.5.7/HTTP1.1）重放**同一字节克隆包 →
    429 `FreeUsageLimitError`**（body/头与 bun 版完全一致，UA 也一致）。
- 头级单变量全部无罪：httpx 自动注入的 `Accept-Encoding: gzip, deflate`
  与"缺失 `Accept: */*`"两个差异点单独重放均 200 → 差异**只剩传输栈**
  （TLS 指纹/JA4+ALPN/h2 语义）。
- 结论：**上游对客户端传输栈本身有判定，非 bun/同栈类一律回以层 2 同款
  429（伪装成配额）**。此前"数百发探针烧穿各 IP 当日额度"的归因不成立
  （探针从不触碰 opencode.ai；且"全新 IP 首发即 429"只有栈门能解释，
  推翻本轮早前"新 IP 到手即被同池租户烧穿"的 CGNAT 共享池假说）。
  官方客户端始终 200 的原因=其内嵌 bun 栈，与 IP 鲜度/形状修复合成无关。
- **修复：网关出网改走 bun 传输子进程**——`proxy_opencode/zen_bun/
  forward.ts`（bun fetch，boringssl/HTTP2/SSE 直通），每次上游试探由
  适配器起一个一次性 `bun forward.ts` 子进程，`HTTPS_PROXY` 指向**当前
  租得桶的 agent**（含 Basic 凭据即时拼装，secret 不落日志/spec），stdout
  直通 SSE、stderr 单行 `META {status,headers}` 供状态分类——池选择/
  cordon/轮换/故障转移语义零改动，仅换"最后一跳"的栈。镜像内置 bun
  1.4.4（npmmirror），`ZEN_BUN_TRANSPORT=1`/`ZEN_BUN_BIN`/`ZEN_BUN_SCRIPT`
  三 env（见 `deploy/ecs/env.example`）；bun 缺席或开关关闭自动回退 httpx
  （dev/test 不受影响）。门禁全绿（pytest 271+33 / ruff / mypy）。
  已知待办：`egress_request` 账本行缺失（当前构建从不写 per-call 行，
  仅 cordon/rotation 事件入流）——观测性缺口，另行修复。

**第四轮定案（08:5x–09:3xZ，session 单次使用——bun 部署后首轮 429 的真因）**：
- bun 部署后首个 hermes 式聊天仍整链 429（每桶 `free_quota_exhausted`
  cordon+轮换），与 bun 传输修复矛盾 → 容器内解耦消融定案：
  - E1：假代理 `127.0.0.1:1` → `ECONNREFUSED`（**bun 尊重 `HTTPS_PROXY`
    env**，排除直连假说）；
  - E2/E4/A：同一次 cc9 防火墙路径，克隆 body + **新 mint 会话** →
    **200×3**（E4 用网关自己 mint 的头 → headers 无罪）；
  - E7/E8/E9/E10：**复用**同一会话（gwheaders）→ 一律 429，无论 body 是
    克隆全量还是最小 4 具——body 无罪，**session 已消费才是触发器**；
  - E5/E6 的 401 `AuthError` 是实验污染（`_zen_model` 只剥 provider
    前缀，别名→裸 ID 映射在 route/catalog 层，实验 body 用了未映射模型）。
- 结论：`x-opencode-session` **单次使用**；网关 `chat()` 旧式"每客户端
  请求 mint 一次、贯穿三条 failover 腿"=对上游的连环重放，坏到第二轮起。
  修复：**in-loop 每试探 mint**；hermes 的 15x 自愈重试是各自独立的
  chat() 调用（每次重新进入 `chat()` 即重 mint 新会话），本就不受影响。
  测试：`test_chat_mints_fresh_session_per_upstream_attempt`（两腿两个
  不同 session）。AGENTS 硬约束 1 增补第四轮条目；该轮起"层 2 429"需
  归因到会话复用或真配额。
- 另证：容器内手动复现走 agent 隧道 + bun + 各桶出口全链路通畅，脚本
  资产（/data/pb/*）与 401/403/429/200 的判定链在 spec 级可复现。

**第五轮定案（2026-10-25 凌晨，owin10 双桶消融——body 形状门 + 免费模型 ID 门）**：
- opc2 永久下线，由 owin10（Windows 10 双上行）双桶顶替后，hermes 链路
  仍整链 429。在 owin10-telecom（106.111.50.124，当日近乎零消耗）上分钟级
  对照消融定案：
  - B0 真 1.19.4 抓包克隆 body（38KB，big-pickle）→ **200**；
  - B3 网关合成 body（bridge 注记 + tiny messages）→ 429；
  - e8：最小 4 合成工具 schema + **克隆 messages** + max_tokens 32000 →
    **200**（工具 schema 内容无罪）；e9：11 真工具 + tiny messages → 429；
  - t5/t6/t7（`deepseek-v4.1-flash-free`）：**system prompt 是判别项**——
    旧资产 prompt（8528B）+ max_tokens 32000 仍 429（t5）；换 1.19.4 实发
    真身 prompt（12779B）→ 200（t6）；真身 prompt + `max_tokens: 50` 亦
    200（t7）。旧资产恰为真身的前缀（新版新增 skills 段）。
- 结论：**body 门 = system prompt 必须是当前真身 opencode prompt**；其 429
  与层 2 配额 429 同形（`FreeUsageLimitError`），无法从响应区分。修复：
  - 资产 `proxy_opencode/upstreams/zen_prompt_default.txt` 8528B → 真身
    抓包（1.19.4 实拍；**入库版 14424B** = 抓包正文 + 捕获时挂载的
    `<env>`/AGENTS 附录，整份随容器部署并经端到端 200 实测
    prompt_tokens≈6529；**严禁截断/清理**——`_default_prompt` 处已钉警示）；
  - `ZEN_MARKER_MODE` 默认 `bridge` → **`full`**（bridge 注记 ~70 token
    过不了门；ECS `.env` 已显式置 `full`）。
- **模型门（401 新形态）**：免费层只服务 `-free` 后缀裸 ID；付费裸 ID
  （`deepseek-v4.1-flash`）同 headers/body 回 **401 `AuthError: Missing
  API key`**（非 403/429，不触发 failover 链）。匿名 GET
  `https://opencode.ai/inference/v1/models`（`Bearer public`）可列免费目录
  （85 个 ID，含 `deepseek-v4.1-flash-free`；GET 不烧额度）。对照实验：
  同 headers 同 body 只改 model 字段，`glm-5.3-flash-free`/`big-pickle`
  200、`deepseek-v4.1-flash` 401。新增 `ZEN_MODEL_MAP` 精确改写；
  `PUBLIC_MODELS` 别名上游改为 `deepseek-v4.1-flash-free`。
- **日志红线修复**：httpx INFO 访问日志打印完整 URL，池探针 URL 的
  userinfo 携带 agent Basic 凭据 → secret 泄入容器日志；`setup_logging`
  将 `httpx`/`httpcore` 压到 WARNING（不得回退，测试
  `test_setup_logging_silences_httpx_access_lines`）。
- 排障注记：本轮 401（付费模型）先于 body 门出现，failover 链不会因 401
  触发；t5 的首跑输出丢失属实验脚手架问题，重测即闭环。

## 1. 当前架构（0.7.x 新栈：ECS 容器网关 + 多桶出口池）

```text
客户端（hermes / 任意 OpenAI SDK）
  -> https://115.29.231.25:10002/v1        # caddy TLS（自签 CA zen-gw-local-ca）
  -> 127.0.0.1:10010（宿主端口映射）
  -> docker 容器 zen-gw（容器内 :10002, uvicorn）
  -> egress 池（按桶 CONNECT 到设备 agent，出口=设备各自公网 IP；上游 TLS/H2
      一跳由 bun forward.ts 子进程承担——传输指纹门，见 §0.5 第三轮定案）
       owin10-mobile http://100.109.109.108:10001 移动 223.68.24.82（bind_source 钉 192.168.1.222；与 win10 同 CGNAT 出口，额度共享）
       owin10-telecom http://100.109.109.108:10002 电信 106.111.50.124（bind_source 钉 192.168.2.235；与旧 opc2-wifi 同公网 IP）
       win10      http://100.121.137.118:10001  电信（家庭 WAN，与 owin10-mobile 同移动 CGNAT 公网 IP；106.111.0.0/16 等，独立传输路径）
       cc9        http://100.87.219.115:10001  联通 112.83.0.0/16;116.147.0.0/16;122.195.0.0/16（2026-10-25 设备下线待恢复）
       （opc2-wifi/opc2-eth 已于 2026-10-25 随 opc2 设备永久下线移除）
  -> OpenCode Zen
```

> **旧栈 9443/zen-lb/cloudflared-ingress 已于 2026-10-17 由所有者下令退役并
> 从 ECS 上彻底清除**（zen-lb / zen-glm53×3 服务停止并删除 unit；`/opt/proxy_opencode`、
> `/opt/proxy_opencode_v2`、`/opt/llmgw`、`/opt/zen-glm53` 目录删除；caddy 仅剩 `:10002`
> 一站；cloudflared 隧道清空 ingress 只剩 404 兜底）。设备侧旧栈完整网关（:879x）
> 尚未拆除，因入口链路已从 ECS 断开，处于不可达闲置状态。

0.7.x 权威描述见 `docs/ARCHITECTURE.md`。新栈对外只暴露一个掩码别名
`deepseek-ai/deepseek-v4.1-flash`（→ 上游免费裸 ID `deepseek-v4.1-flash-free`，
2026-10-25 前为 `big-pickle`；
2026-10-17 起严格别名：上游裸 ID 不可请求、不可见，除隐藏端点
`/v1/models_cus` 纯裸 ID 输出）。四桶中 win10 与 opc2-wifi 出口同一家庭 WAN
（同一公网 IP 的上游额度共享，但独立一条 ECS↔本机的传输路径；2026-10-25 起
win10 与 owin10-mobile 同移动 CGNAT 出口，同享额度）；容器内
least-in-flight 选择、429/5xx/传输失败**桶间 failover**、cordon 冷却、P5 每窗口
≥500 或硬 429 自动换 IP（2026-10-17 起三桶 ONT 驱动全部实弹验证，见下表）。ECS/Tailscale 地址与凭据位置见 `ACCESS.md`，
不要把真实密钥写入仓库。

## 2. 部署与最近状态（0.7.x 新栈，最近部署 2026-10-25）

| 桶 | agent | 出口 IP | expect_prefix | 最近确认状态 |
|---|---|---|---|---|
| `cc9` | `100.87.219.115:10001` | `112.83.208.101` 联通 | `112.83.0.0/16;116.147.0.0/16;122.195.0.0/16` | healthy；HG6142A-C `fenghuo` 驱动实弹受理通过（112.83.208.127→116.147.31.99→116.147.30.191→122.195.202.2）；chroot agent 由 Magisk 看门狗拉起；`122.195.0.0/16` 为后续轮换落到的联通新段（2026-10-17 补入）；2026-10-19 06:39Z 门槛标记后轮换所得新 IP；**2026-10-25 设备下线待恢复** |
| `win10` | `100.121.137.118:10001`（Windows 本机 egress-agent，`.venv` python + `deploy/windows/start-egress-agent.ps1`，Startup 自启） 移动 CGNAT 公网 IP 族（与 owin10-mobile 同出口） | `106.111.0.0/16;121.226.0.0/16;106.110.0.0/16;180.117.204.0/24;114.239.0.0/16` | healthy/probe ok；CONNECT 数据面真测通过（经本机隧道对 opencode.ai 200，2026-10-24 部署）；**无 ONT、不参与轮换**（不入 `ZEN_EGRESS_ROTATION`）；当日窗口配额随（同 WAN 共享的）IP 已烧穿（见 §0.5） |
| `owin10-mobile` | `100.109.109.108:10001`（Windows 10 owin10 有线口，`bind_source 192.168.1.222` 钉源出网，schtasks `EgressAgent-mobile` ONSTART SYSTEM 自启） | `223.68.24.82` 移动 | `223.68.0.0/16;36.149.0.0/16` | healthy（2026-10-25 部署，healthz+CONNECT 真测通过）；**无 ONT、不参与轮换**；与 win10 同 CGNAT 出口（上游额度共享）；当日窗口配额已烧穿（见 §0.5） |
| `owin10-telecom` | `100.109.109.108:10002`（owin10 WiFi 口，`bind_source 192.168.2.235`，schtasks `EgressAgent-telecom`） | `106.111.50.124` 电信 | `106.111.0.0/16;121.226.0.0/16;106.110.0.0/16` | healthy；2026-10-25 body 门/模型门消融主力桶（克隆 200、合成 429、free-ID 200 全链定案，见 §0.5 第五轮）；**无 ONT、不参与轮换** |
| `ecs` 本机桶 | （已剔除） | 115.29.231.25 | — | **2026-10-17 移出池**（机房 IP 免费额度被共享流量即耗，实测"几条就429"主因）；`egress-agent@ecs` 已停用 |
| `hangzhou-ecs` | docker `zen-gw`（caddy→127.0.0.1:10010→容器:10002） | 115.29.231.25 入口 | — | healthy；failover 真机首测通过（wifi 429 → 换桶 → 客户端 200,2026-10-17 17:02 UTC） |

2026-10-05 的 LB 池调整记录与 `edge_lb.env` 变更备份已随旧栈退役（2026-10-17 ECS 清理）一并移除；
历史细节见 git 历史与 `CHANGELOG.md`。

### 2.1 0.7.x 新栈部署与最近状态（2026-10-16 idx 全绿）

- **ECS**：`/opt/zen-gw/src`（deploy 快照，非 git 树）`docker compose up -d
  --build` 成功；宿主 `127.0.0.1:10010 → 容器 10002`，caddy `:10002` 反代
  10010。healthz ok。`/data` 属主 uid 10002（eventlog 写失败自动禁用，权限
  修复后必须重建容器）。
- **出口桶（统一 IPv4 前缀，四桶全 healthy）**：opc2-wifi
  `180.117.204.212`（ISP 已漂移，前缀响应更新为 `180.117.204.0/24`）、
  opc2-eth `36.149.54.30`（移动有线，见下方事故修复）、cc9
  `112.83.208.127`（联通改走 IPv4，前缀 `112.83.208.0/24`）、ecs
  `115.29.231.25/32`。
- **公网入口 smoke（Windows → https://115.29.231.25:10002）**：/v1/models 仅
  公开别名（标准 OpenAI 形状）；`/v1/models_cus?key=` 被反代上游裸 ID 目录
  （无 key → 401）；chat 别名 200；裸 ID 404、错 model 404 / 缺 model 400 /
  错 key 401——全部符合预期。
- **opc2 有线腿事故（已修复）**：旧栈策略路由 provision 单元（991/uid 996、
  410/uid 997）为 boot oneshot，规则与表默认路由被上游事件吐掉后 eth agent
  回落 wifi 出口（180.117.204.212），被池身份校验连续拦截。修复：重跑两个
  provision 单元（uid 996 出口恢复 36.149.54.30）+ 加装
  `opc2-policy-routes-keep.timer`（开机 2min + 每 10min 幂等重放，覆盖 ONT
  重启 / P5 eth 轮换后的恢复窗口 ≤10min）。eventlog `identity_mismatch`
  于修复后终止，转为 `egress_ip_observed` 迁移事件。
- 已知非问题：healthz 版本串仍显示 `0.7.0a1`（陈旧显示）；`llm.223722.xyz:10002`
  域名入口未配置，IP 字面量入口可用。

## 3. 验证与常用操作

```bash
# ECS 上（SSH 见 ACCESS.md；容器宿主 10010 -> 容器 10002，caddy 对外 10002）
docker logs zen-gw --since 30m 2>&1 | grep -E "chat completion|stream stalled|failover|cordon"
docker compose -f /opt/zen-gw/src/deploy/ecs/compose.yml up -d --build   # 改代码后重建

# 桶状态 / 手动换 IP（管理员 key，仅 Tailscale）
curl -s http://127.0.0.1:10010/admin/egresses -H "Authorization: Bearer $ADMIN_KEY"
curl -X POST http://127.0.0.1:10010/admin/egresses/<桶名>/rotate -H "Authorization: Bearer $ADMIN_KEY"

# 桶请求量统计：从 egress_request JSONL 读取，必须带 bucket_name + egress_ip
# （字段口径与 jq/python 示例见 docs/EGRESS_OBSERVABILITY.md）
```

免费层协议事实与 429/403/401 判读的**唯一权威表述在 `AGENTS.md`** 硬约束 1；
配额探测不做常规化（活体 200 烧额度，见 AGENTS 排障红线）。

项目质量门禁（与 AGENTS §3 一致，另加 `python scripts/e2e_protocols.py` 用
mock upstream 做协议端到端验证）。真实 Hermes 推理是额外的环境测试，通常
会消耗上游额度。

## 4. 排障纪律

免费层协议事实（多层门：形状/传输栈/session 单次使用/body/模型，403/429/401
判读、裸模型 ID）的**唯一权威表述在 `AGENTS.md`** 硬约束 1，本节不重复。
链路排障要点：

- 断流/429/401 先按 AGENTS 五门归因（形状→传输栈→session 复用→body→模型→
  真配额），再看桶：`/admin/egresses` 看各桶状态与出口 IP，`docker logs
  zen-gw` 看 `bun upstream attempt: proxy=` hop 记录与 cordon/failover。
- 不要用真实用户密钥做批量探测；不要把请求正文、prompt 或 key 写入日志。

## 5. 网络事件记录：opc2 到 Cloudflare，以及直连入口

### 5.1 移动网 ↔ Cloudflare Anycast（2026-10-02/03）

出口实测：`opc2` 与 `owin10` 是**同一出口 `36.149.54.82`（移动，江苏宿迁）**；`phone115` 出口
`117.95.231.70`（**电信**，同城）。三机同城，故对照实为「移动 vs 电信」。

用 `curl --resolve` 强制同一 Cloudflare IP 的受控实验：

| 目标 IP | 手机（电信） | opc2（移动） | owin10（移动） |
|---|---|---|---|
| `104.21.89.73` | 5/5，~1.5 s | 5/5，0.6–6.6 s | 5/5，0.8–3.3 s |
| `172.67.156.243` | **5/5，~1.5 s** | **3/5 超时** | **4/5 超时** |

真实 chat 到 `https://llm.223722.xyz/v1`：手机（电信）5/5（4–14 s）；opc2（移动）仅 **2/5**。

**结论**：故障域是「**移动 ↔ 特定 Cloudflare Anycast IP（`172.67.156.243`）**」的对等/路由问题，
不是 ECS、LB、四桶或 Cloudflare 全局——电信命中同一 IP 完全正常。已做的缓解：opc2 设 IPv4 优先
（`/etc/gai.conf` + Hermes `network.force_ipv4: true`）、Wi-Fi MTU 2312→1480；opc2 经 Tailscale
直连 ECS LB `100.81.214.95:7892` 为 6/6、约 38–41 ms。

### 5.2 直连入口被拦的根因：阿里云按域名拦截（2026-10-03）

直连 ECS 的明文 HTTP 比经 Cloudflare 快约 100–300 倍（约 45–50 ms、无丢包），但**只要带上
`223722.xyz` 域名就全端口被拦**。完整探测矩阵（从 opc2 发起）：

| 探测 | 结果 |
|---|---|
| 明文 7892，`Host: 115.29.231.25` | 200 |
| 明文 7892，`Host:` 任意 `*.223722.xyz` | **403 `Server: Beaver`** |
| 明文 7892，`Host: www.baidu.com` | 200 |
| `openssl s_client -connect 115.29.231.25:9443`（**无 SNI**） | 握手成功 |
| 同端口 SNI = `zen`/`llm`/`223722.xyz` 任一 | **连接被 RST** |
| 同端口 SNI = `www.baidu.com` / `example.com` | 握手成功 |

**根因**：杭州 ECS 在中国大陆，阿里云 ICP 合规拦截按 `Host` 头 / TLS `SNI` 的**域名**生效、
**不限端口**；`223722.xyz` 整域未备案 → 全端口被封。`llm.223722.xyz` 能用只是因为 Cloudflare
在**边缘**终止 TLS、cloudflared 由 ECS **主动出站**，阿里云从未见到入站该域名。（§5.1 的
「移动 ↔ 特定 CF Anycast IP」是叠加的另一层问题，两者互不替代。）

**解决**：客户端连 **IP 字面量**（不发送 SNI）→ 不触发拦截；ECS caddy 用 catch-all 站点 +
自签证书（SAN 含 `IP:115.29.231.25`）在 `:9443` 服务（该入口已随旧栈清退，
现役入口见 `ACCESS.md` §1）。**客户端接法与证书信任见
`ACCESS.md`**，此处不重复。

**实测**（各 20 次）：`https://115.29.231.25:9443` **20/20**（约 70–80 ms）；
`https://llm.223722.xyz` **18/20**。

**遗留**：要让 `zen.223722.xyz` 成为正式**域名**入口，只能做 **ICP 备案**，或把入口迁到
**非大陆节点**（港/新，免备案）。当前策略是「CF + IP 直连」双入口。

## 6. 当前风险与待办

1. **`egress_request` per-call 账本缺口**：当前构建从不写 per-call 行（仅
   cordon/rotation 事件入流），按桶统计请求量暂无数据源（见
   `docs/EGRESS_OBSERVABILITY.md` 设计稿）。观测性缺口，另行修复。
2. **cc9 设备下线待恢复**（2026-10-25）：恢复前出口池只有 owin10 双桶 +
   win10，且 owin10-mobile 与 win10 同移动 CGNAT 出口（`223.68.24.82`，
   上游按 IP/UTC 日计量额度共享）——同出口桶越多，单 IP 额度越快烧穿；
   owin10-telecom（106.111.50.124）是当日唯一独立新鲜 IP。
3. **容器时区**：容器为 UTC（计数窗口、日志 ts 均为 UTC）；如需按
   Asia/Shanghai 对账，加 `TZ` 环境变量并重建（P5 计数窗口本身按
   Asia/Shanghai 计算，不受影响）。
4. **公网入口的域名化**：IP 直连入口需装自签 CA（新增客户端的信任分发是
   常驻运维点，证书重建需重新分发）；域名入口需 **ICP 备案**或**迁非大陆
   节点**（历史结论见 §5.2）。
5. **公开 `/healthz` 暴露桶拓扑与错误摘要**：后续应评估收至管理员接口或
   脱敏公开响应。

## 7. 单网关 + 多出口槽位：已落地为 0.7.x 新栈

曾评估用杭州 ECS 上的单网关 + 多出口槽位替代四台异构机器各跑完整网关。该方案
**已落地**为 0.7.x 新栈：网关进 ECS Docker 容器，桶设备只跑 `egress-agent`，客户端
入口 `https://115.29.231.25:10002/v1`。权威描述（端口注册表、分期状态、ADR、ONT
换 IP 拓扑）见 `docs/ARCHITECTURE.md`，客户端接法与证书见 `ACCESS.md` §1。

旧栈（`llm.223722.xyz` + `9443` + zen-lb + 四桶完整网关）的 ECS 侧已于
2026-10-17 彻底清退（设备侧 `:879x` 旧网关因入口链路断开而不可达闲置）。
历史文档里记的 `llm2.223722.xyz` 计划命令与端口**不是**新栈的操作步骤，
勿混用。
