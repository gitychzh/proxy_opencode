# 架构总册（ARCHITECTURE）

> 本文件是 **0.7.x 新栈**的唯一权威描述：网关整体搬进 ECS 的一个 Docker
> 容器，配额桶设备只跑 `egress-agent`（设备侧出口代理）。
>
> **单栈（2026-10-25 现状）**：旧栈（ECS `zen-lb` + 各设备完整网关部署，
> 入口 `9443`）已于 2026-10-17 从 ECS 彻底清退（详见 §7 末尾注记）；
> hermes 现走新栈入口 `https://115.29.231.25:10002/v1`。

---

## 1. 目标形态

```
hermes / codex / 客户（OpenAI 兼容客户端）
   │ 新入口:  https://115.29.231.25:10002/v1   (IP + 自签CA, hermes 用)
   │          https://llm.223722.xyz:10002/v1  (域名 + LE真证书, 客户零安装)
   ▼
ECS ┌──────────────────────────────────────────────────────────────┐
     │ caddy :10002 (TLS) ──▶ 127.0.0.1:10002                       │
     │ docker: zen-gw（proxy_opencode 0.7.x，本 repo 唯一部署点）    │
     │  ├─ routes/（chat/models/responses/anthropic/admin）          │
     │  ├─ upstreams/zen_direct.py（协议/标记/头部/聚合, 0.6.5 行为） │
     │  ├─ sse_mask.py（流式切断合成 finish=length, 0.6.5）          │
     │  └─ egress/                                                   │
     │     ├─ pool.py     每出口一个 httpx client(proxy=agent)       │
     │     │          least-inflight(rr) + cordon + /healthz 探测    │
     │     ├─ counters.py 日窗计数(08:00 CST 重置) ≥500 → 换 IP       │
     │     ├─ rotate.py   换 IP 编排（调 agent /rotate, 验 IP 变更） │
     │     ├─ catalog.py  Zen 反代模型目录（白名单交集）+ TTL 缓存        │
     │     ├─ eventlog.py 结构化事件日志（JSONL 落 /data）           │
     │     └─ updater.py  每日 02:00 看 opencode GitHub release      │
     └───────┬───────────┬───────────┬───────────┬──────────────────┘
        tailscale 100.x │           │           │
        ┌──────────────┴┐  ┌────────┴─┐  ┌──────┴─────┐
        ▼               ▼  ▼          ▼  ▼            ▼
   owin10:10001(mobile) owin10:10002(telecom) win10:10001  cc9:10001
   egress-agent         egress-agent       egress-agent egress-agent （Windows 本机桶=同一 Python 包 + schtasks/Startup 自启）
   （Python, 每台上行仅一个代理进程; connECT 出去=本设备公网IP;
     owin10 双实例用 bind_source 钉源：mobile=192.168.1.222→移动 223.68.24.82，
     telecom=192.168.2.235→电信 106.111.50.124；opc2 双桶 2026-10-25 随设备
     退役移除；cc9 2026-10-25 下线待恢复）
        │               │              │            │
        └───────────────┴──────────────┴────────────┴──▶ opencode.ai/zen/v1
```

**为什么成立**：Zen 免费层判定皆在 wire 层、由 ECS 网关侧供给，与出口
网段无关：**形状门 403**= descending session ID + ≥4 真名内置工具 +
`stream:true`（2026-10-24 消融定案；UA/头名/schema 内容不敏感）；
**配额门 429**=每出口 IP/UTC 日（Retry-After 指向 UTC0）；**传输栈门
429**（2026-10-24 PM 第三轮定案）=非 bun/同栈客户端一律伪装配额 429，
与 IP 鲜度无关。为此上游一跳改走 bun——`proxy_opencode/zen_bun/
forward.ts` 一次性子进程（boringssl/HTTP2，镜像内置 bun 1.4.4；
`ZEN_BUN_TRANSPORT=1`），每次上游试探以**当前租得桶的 agent** 为 HTTPS
代理出站（`_egress_proxy_url` 即时拼装 Basic 凭据；httpx 仅 dev/test
回退）。agent 的 CONNECT 出口就是各设备公网 IP，四桶隔离原样保留；
字节量逐跳一致（ECS↔设备段=网关→子进程→代理 CONNECT），带宽与移动
流量零新增（子进程 spawn ~10ms 量级/请求）。

## 2. 端口注册表（统一口径）

| 端口 | 归属 | 说明 |
|---|---|---|
| **10001** | 设备侧 egress-agent | 代理端口（CONNECT + /healthz + /rotate 同端口）。owin10（mobile/telecom 双实例）/ win10 / cc9 |
| **10002（设备）** | 设备侧 egress-agent 第二实例 | 同一台设备第二上行专用（owin10-telecom）；**同时是 ECS 对客户端入口**（caddy:10002 → 容器 127.0.0.1:10002） |
| **10003** | 预留 | 未来出口或第二服务 |
| 9443 / 7892 / 8793 / 46667 等 | 旧栈端口 | **已于 2026-10-17 从 ECS 清退**；设备侧 8791 旧本机网关不可达闲置 |
| 40016 / 40066 | GLM-5.3 / GLM-5.3F 网关（ECS 现有服务） | 与新栈无关，不动 |

安全组：ECS 入站新增 10002（与 9443 同法）；设备侧 10001/10002 只听
Tailscale 100.x（systemd 绑定 0.0.0.0 但靠 tailscale 网络隔离，另加
agent 代理 basic auth 双保险）。

## 3. 组件地图（repo 布局）

| 路径 | 内容 | 阶段 |
|---|---|---|
| `proxy_opencode/egress/` | pool / counters / catalog / updater / rotate | P2-P5 逐个落地，P0 为契约桩 |
| `egress-agent/` | Python 包：config / cli / logging（P0 真实）+ proxy / egress / health / rotate（P1） | P1 |
| `egress-agent/deploy/` | systemd 模板单元、安装脚本、配置模板；Windows 桶（owin10/win10）部署见 `deploy/windows/README.md`（schtasks + `bind_source` 实测路径） | P0 |
| `egress-agent-android/` | Kotlin 原生版占位（wire 契约已冻结） | P6 |
| `deploy/ecs/` | Dockerfile/compose（P2）、caddy 10002 片段（含 LE DNS-01 说明） | P0/P2 |

## 4. 分期与状态

| 阶段 | 版本 | 内容 | 状态 |
|---|---|---|---|
| P0 | 0.7.0a1 | 冻结方案、repo 骨架、CI 双端门禁、端口注册表、caddy 片段 | **已完成**（7474c22，含既有 CI 失败修复） |
| P1 | 0.7.0 | egress-agent 数据面（CONNECT/healthz/rotate）+ 设备实测 | **已完成**（双桶出口/换 IP 真机验证通过） |
| P2 | 0.7.0 | 网关 egress pool + Docker/compose，ECS:10002 起容器；各桶出口逐一实测复核 | **已完成**（opc2-wifi / opc2-eth / cc9 三桶真机跑通；pad 待凭据延后，见下） |
| P3 | 0.7.0 | catalog 反代白名单（默认仅 big-pickle）+ 隐藏 /v1/models_cus；hermes 加新 alias（旧 alias 保留） | **已完成**（2026-10-17 收敛为严格别名：裸 ID 不可请求） |
| P4 | 0.7.1 | updater：每日 02:00 查 GitHub release（含 prerelease），分级采用 | 未开始 |
| P5 | 0.7.2 | 换 IP：计数器窗口 + 500 触发 + ONT 重启 + IP 变更验证 + 429 兜底 | **已完成**（2026-10-17 三桶 ONT 驱动全部实弹验证，见 §6） |
| P6 | 0.8.0 | Android Kotlin 版、文档浓缩定稿 | 未开始（Windows agent 已先行落地：win10 2026-10-24、owin10 双桶 2026-10-25，见 `egress-agent/deploy/windows/README.md`；旧栈 ECS 侧已于 2026-10-17 清退） |

### P5 现状（2026-10-16，ECS 侧代码完成，设备侧待真机）

- **已实现**（`proxy_opencode/egress/counters.py` + `rotate.py`，25 个新测试全绿）：
  每桶日窗 `[08:00, 08:00+1d)` Asia/Shanghai 计数（窗口自动滚动归零）；
  阈值（`ZEN_EGRESS_ROTATION`，逐桶配置，如 `opc2-eth=500`）触发**每窗口恰好
  一次**换 IP；上游 429（`Retry-After > 3600s`，或 **完全没有
  `Retry-After` 头**——2026-10-16 实测：配额 429 可不带该头，当天三次
  cordon 全是 `retry_after_s: null`，两形都判 per-IP 日耗尽），或
  **匿名 403 `FreeTierError`（出口 IP 被门槛标记，2026-10-17 实测）**，
  **绕过阈值立即换 IP**，且这些 bypass 都与阈值共用同一"每窗口一次"
  去重 flag（`note_rotation_started` 置位，防光猫被连环重启）；**硬 429
  成功换 IP 后还有 30 分钟最小间隔**（`ZEN_EGRESS_ROTATE_MIN_INTERVAL_S`，
  默认 1800，0=关闭）——429 四因同形，只有真实 per-IP 配额能靠新 IP 解决，
  2026-10-25 的单日 32/20/13 次换 IP 风暴即把非配额 429 误当配额所致；
  编排流程=标记 rotating（选桶跳过、在途请求自然
  收尾）→ `POST {agent}/rotate` → 轮询 `/healthz` 等 IP **变化且落在
  `expect_prefix` 内**（同 IP 重发/腿回落都判失败，大声记录）→ 成功后清
  cordon（含配额 cordon）、计数归零重计。**`POST /rotate` 传输失败不判最终
  失败**：命令可能已送达 agent（或 ONT 自行重拨），照常轮询到 deadline，
  观测到合法新 IP 即按成功收敛（2026-10-24 实测：隧道抖动令 `POST` 30s
  超时报 `agent unreachable`，桶 IP 随后真实更换——明确的 HTTP 拒绝仍是最终
  失败）。计数器状态 JSON 快照
  （`ZEN_EGRESS_COUNTS_PATH`，容器 `/data` 卷）跨重启存活。
- **cordon 的 IP 语义（2026-10-24 增补）**：配额烧穿（429 →
  `free_quota_exhausted`，绑到窗口末）与门槛标记（403 →
  `free_tier_rejected`）都是**对出口 IP 的判决**：探测循环观测到 agent
  上报的出口 IP 变化、新 IP 落在 `expect_prefix` 内且探针就绪时，这两类
  cordon **立即解除**（事件 `cordon_lifted`），不让健康的新 IP 坐满全窗口；外
  前缀 IP（eth 腿回落 wifi 出口）与探针未就绪不解除；transport 连败 streak
  cordon 保持原 deadline；新 IP 若同样烧穿，会在第一个请求上重新 cordon。
- **`/admin/egresses`** 快照新增 `counters` 字段；新增
  `POST /admin/egresses/{name}/rotate`（管理员 key）= 手动触发，用于现场
  调试 wifi/cc9 光猫 API 与"IP 被重发同值"后的补一枪。
- **设备侧 ONT 驱动**（2026-10-17 落地，2026-10-25 owin10 双桶接续）：opc2-wifi
  HG8020C `kind="huawei_ct"`、cc9 HG6142A-C `kind="fenghuo"`（协议细节与实弹
  记录见 §6 与 git 历史）；2026-10-25 起现役为 owin10 双桶——`owin10-mobile`
  V173-50 `kind="huawei"`、`owin10-telecom` HG8020C `kind="huawei_ct"`，两份
  stanza 在设备 `C:\egress-agent\{mobile,telecom}.json`（`interface` 写字面
  IPv4，见 §6 坑④），实弹轮换各成功一次。
  opc2 现有"ONT 每日 7 次定时重启" cron 必须在启用 `ZEN_EGRESS_ROTATION`
  时停掉，否则双重换 IP。

### P1 实测结论（2026-10-14，opc2 真机）

- agent 出口绑定：wifi 用 `bind_source`（源 IP）可行；**eth 必须用 uid 996
  策略路由**（与旧网关同机制，systemd `User=opc2-bucket-b`），bind_source
  会让连接直接失败。
- ONT 重启换 IP 全链通过（36.149.54.158 → .30），但 eth 腿恢复需要
  DHCP 续租 + 策略表默认路由补回；恢复期间 uid 进程会回落 wifi 出口
  （危险：静默消耗别的桶额度）→ P5 的池必须带 `expect_prefix` 桶身份校验。

### P2 实测结论（2026-10-14，ECS 容器 + 三桶真机）

- **部署形态**：镜像 `zen-gw:0.7`（python:3.12-slim）在 ECS 以 `docker run`
  运行（`compose.yml` 为等价交付物；ECS 上 docker compose 插件未装，暂用
  `docker run`，参数与 compose 一致），发布到 `127.0.0.1:10002`，只读根 +
  `/data` 卷，`healthy`。对外 caddy `:10002` + 安全组留待需要时启用。
- **三桶出口逐一实测**：`/admin/egresses` 三桶 `healthy=true`、出口 IP 落在
  各自 `expect_prefix` 内；6 并发请求 2/2/2 均匀落到三桶、全 `200`、零失败；
  串行与非流式走 `_aggregate_sse` 聚合正常，流式 SSE 透传正常。
  - opc2-wifi `117.95.231.70`（电信 IPv4，bind_source）、opc2-eth
    `36.149.54.30`（移动 IPv4，uid 策略路由）、cc9
    `2408:823c:be11:313d:…`（联通 IPv6）。
- **cc9 用 Android chroot（Ubuntu 24.04 aarch64 + 自带 python3.12）跑 Python
  agent**，非 Kotlin：设备端 `/opt/egress-agent/venv` + Magisk `service.d`
  watchdog（`egress-agent-watchdog.sh`，3 次健康失败即重启；真机杀死 agent
  后 ~90s 自动恢复已验证）。**出口是 IPv6-only**：出站连接靠 asyncio 按
  getaddrinfo 顺序 IPv4 快速失败→回落 IPv6，实测隧道正常；`expect_prefix`
  用 IPv6 `/64`。
- **部署期发现并修掉的集成缺陷**（都已进测试）：① 池 client 无 `base_url`
  时 zen_direct 相对路径 → 每个池请求报 “missing http:// protocol”；② 池
  没给 CONNECT 带认证 → agent 407（`httpx.Proxy` 注入 secret，不进快照）；
  ③ 设备系统 Python <3.12 且 `$HOME` venv 被 `ProtectHome` 挡 → agent 装
  `/opt/egress-agent/venv`；④ systemd 不允许对 ExecStart 命令做变量展开。
- **pad 桶延后**：`xiaomi-pad-5u`（Linux，218.93.215.38）缺 SSH 凭据，所有者
  指示先做 opc2×2 + cc9 三桶，pad 以后再说（链路已通，接桶=装一个代理软件）。

### P2 追加实测（2026-10-15，ECS 本机作为第 4 桶 + 选桶轮询修正）

- **ECS 本机也是第 4 个配额桶**：ECS 上装 `egress-agent@ecs`（systemd，
  venv `/opt/egress-agent/venv`，config `/etc/egress-agent/ecs.json`），监听
  **`172.17.0.1:10001`**——docker bridge 网关 IP，容器经它到达（**不是**
  `127.0.0.1`），出口即 ECS 公网 IP `115.29.231.25`（`expect_prefix` 为 `/32`）；
  容器 `.env` 追加 `ecs=http://172.17.0.1:10001`。`/admin/egresses` 四桶全绿。
- **顺序选桶钉死单桶（已修，见 CHANGELOG）**：`/admin/egresses` 暴露顺序请求
  全部落在 `cc9`（其余桶 `total=0`）——原 `min(inflight, name)` 在“全空闲”这个
  顺序请求的常态下恒取字母序最前的桶。改为 balancer 同款
  `itertools.count()` 轮转 + 稳定 least-inflight；24 并发复测四桶分摊、全 200。
- **部署坑：CRLF 污染 agent 凭据**：`/etc/egress-agent/<实例>.json` 若从一个
  CRLF 文本生成，Basic 凭据尾部会带一个 `\r`（`len` 比预期多 1），与容器里的
  干净 secret 不等 → agent 对每个 CONNECT 回 **407** → 池记为 `transport`
  失败 → 502（真机表现：该桶 5 次请求 4 次 502）。**建 agent 配置时用 LF 源，
  并校验凭据长度**（`install.sh` 本身不写凭据，是手写步骤踩到的）。

### 公开模型目录（2026-10-16 起；2026-10-17 收敛为单模型严格别名）

- **别名**：默认 `deepseek-ai/deepseek-v4.1-flash` → 上游免费裸 ID
  `deepseek-v4.1-flash-free`（2026-10-25 起经 `ZEN_MODEL_MAP` 改写，精确命中
  优先于 provider 前缀剥离；此前上游为 `big-pickle`，现退场）。`/v1/models`
  返回严格 OpenAI 形状（`id/object/created/owned_by`，2026-10-17 起
  不再带 `display_name`），hermes 等 agent 直接自动发现；**只有 `PUBLIC_MODELS`
  里的对外 id 可以被请求**，上游裸 ID 一律 404（`registry.resolve` 不再接受
  上游拼写）。
- **被反代模型白名单**：`OPENCODE_ZEN_MODELS` 是网关唯一反代的上游模型集合
  （当前部署为 `deepseek-v4.1-flash-free`）。
  `egress/catalog.py` 每 10 分钟拉
  `GET https://opencode.ai/zen/v1/models`，按上游自带标记
  （`allowAnonymous`/`cost`/`pricing`；缺标记时按 `-free` 后缀）
  判定免费，再与白名单求交集——2026-10-17 起不再枚举"全部免费模型"，也不再有
  裸 ID 透传（聊天接口对上游裸 ID 返回 404）。
- **`/v1/models_cus`（隐藏端点）**：唯一展示被反代上游裸 ID 的地方；鉴权接受
  Bearer 或 `?key=`（浏览器直接打开）。响应默认为一行一个裸 ID 的
  `text/plain`（无尾随换行、零装饰；owner 指令 2026-10-16），`?format=json`
  才返回 OpenAI 形状（data/metadata）。**标准 `/v1/models` 保持只列公开
  别名**，不要再造 models/model 等普通变体端点（防被模型扫描 agent 捕获）。

### 备用链路（2026-10-25 落地）：zen-direct 故障 → proxy_ms

网关在内置出口池之上再挂一条**跨供应商备用链路**：主链路 `zen-direct`
（OpenCode Zen 免费层）发生**出口级**故障时，请求自动改由同一台 ECS 上的
`proxy_ms`（ModelScope 聚合网关，宿主 `172.17.0.1:10003`，见
`docs/OPERATIONS.md` §2.3）服务，故障窗口结束后自动回到主链路。

- **实现形态**：`upstreams/failover.py` 的 `FailoverAdapter` 包在
  `build_adapter` 的原生适配器外面（一条包装覆盖 chat/completions、
  completions、responses、/v1/models 全部表面）；`ZEN_FALLBACK_BASE_URL`
  为空时完全不包装，行为零变化。
- **触发条件（仅出口级）**：`ConnectionError`（隧道/上游不可达），或上游
  错误中继的状态码 ∈ {429 配额, 500/502/503/504}。**请求级 4xx
  （模型不存在、鉴权失败）不切换**，原样回给客户端——那是客户端的问题，
  换供应商只会把错误变成另一种错误。503 含 zen 适配器自己的
  `egress_unavailable` 出口闸门（全池耗尽）。
- **熔断语义**：触发后进入 `ZEN_FALLBACK_HOLD_S`（默认 120s）保持窗口，
  窗口内**不再打主链路**（避免给已烧穿/被标记的出口继续施压），全部走备用；
  窗口后第一个请求对主链路做**半开探测**——成功即闭合并记录
  `FALLBACK RECOVERED`，失败则重新进入保持窗口。
- **模型 ID 改写**：备用网关用 ModelScope 目录 ID（如
  `deepseek-ai/DeepSeek-V4.1-Flash-1017`），与 zen 裸 ID 不可能重合。
  **注意包装层拿到的是路由层已解析的 ID**（公网别名
  `deepseek-ai/deepseek-v4.1-flash` 到这里已是 `big-pickle`），所以
  `ZEN_FALLBACK_MODEL_MAP` 的键必须是解析后的上游 ID；
  `ZEN_FALLBACK_DEFAULT_MODEL` 兜住所有未命中项，避免把 zen 裸 ID 丢给
  备用网关被白名单拒掉。
- **醒目提醒（三处，全部不含消息内容/密钥，符合日志红线）**：
  1. 日志 WARNING `FALLBACK ENGAGED` / `FALLBACK ROUTING` /
     `FALLBACK RECOVERED` / ERROR `FALLBACK FAILED`（备用也挂）；
  2. `egress_request` 同源的 JSONL 事件 `fallback_engaged` /
     `fallback_recovered` / `fallback_failed`；
  3. `GET /healthz` 新增 `fallback` 块：
     `{"engaged": true, "secondary": "...", "hold_remaining_s": 119.1}`。
- **双侧全挂**：备用不可达时记 `FALLBACK FAILED` 并抛 `ConnectionError`
  （客户端可重试语义），熔断状态保留，不会因备用失败而误判主链路已恢复。
- **备用返回错误时释放保持窗口（2026-10-25 修正）**：备用不是"必定可用"。
  实测 proxy_ms 自身欠费回 `429 insufficient balance`，客户端会把它判成
  `billing / retryable:false` 并放弃——而主链路可能只是被四种同形 429
  之一误伤（传输栈/prompt 快照/session 复用），换 IP 或修复后已恢复。
  因此备用返回错误中继（非连接失败）时：**原样回给客户端**
  （诚实暴露备用故障）、记 ERROR `FALLBACK FAILED: ... answered HTTP <n>`
  + JSONL `fallback_failed{status}`，并**把熔断窗口清零**，让下一个请求
  重新探测主链路。备用不可达（`ConnectionError`）仍保持熔断（上一条）。

## 5. 关键决策记录（ADR 摘要）

| 决策 | 结论 | 理由 |
|---|---|---|
| agent 语言 | ubuntu/windows=**Python**；android=**Kotlin**（占位） | 稳定优先、成熟优先；现有设备全部可跑 Python（含 Android chroot） |
| 第三方依赖 | 用成熟库不自造轮子：HTTP 解析=aiohttp，ONT 重启=requests；唯独 CONNECT 双工管道自写 ~100 行 | proxy.py 评估后弃用：过重且源地址绑定能力不明确；依赖全部 pip 装本机，运行期零云端调用 |
| TLS/证书 | IP 入口沿用自签 CA（hermes 已 pin，零成本）；客户入口=域名 + LE 真证书（DNS-01，A 记录灰云直指 ECS） | 公共 CA 不签裸 IP；域名走 CF 隧道会绕海外（实测慢 10 倍），灰云直连 ECS 后是国内直连 |
| 计数器语义 | 每出口、日窗 [08:00, 08:00+1d) Asia/Shanghai（=UTC0 重置）；窗内 ≥500 触发换 IP；换完计数清零（新 IP=新额度桶）；**2026-10-24 修正：上游真实匿名日额度 ≪500 且按成功调用计**（win10 家宽十几发成功即 429-烧穿，Retry-After 62799s 精确指向 UTC0），500 阈值仅作存在性兜底 | 上游 per-IP 日配额 UTC0 重置；用户确认的语义+2026-10-24 实测 |
| 429 兜底 | 上游 429 且（`Retry-After > 3600s` **或无 Retry-After 头**）= per-IP 日耗尽 → 立即换 IP（绕过 500 阈值，每窗口每桶至多一次） | 2026-10-16 实测补全：配额 429 可完全不带头（当天三次 cordon 全 null，0 轮换）；有头短值=短窗软限流（只 cordon 不换 IP） |
| 403 门槛兜底 | 匿名模式下上游 403 `FreeTierError` → 释放租赁为失败（`free_tier_rejected`）、**立即 cordon 该桶**并**绕过阈值立即换 IP**（每窗口每桶至多一次，共用硬 429 的去重/失败冷却）。**2026-10-24 重解读：主因是形状门（抽包消融证明 = 工具名 <4 真名，请求级），非单纯"IP 被标记"**；网关侧已按 ≥4 真名注入自愈，cordon/failover 仅作防御保留（若上游再收紧可自恢复） | 此前 403 被当作"正常工作出口的请求级拒绝"按 ok 释放 → 桶一直"健康"地返回 403，用户持续报错；2026-10-24 字节级克隆消融测得：**形状缺陷在干净出口上也 403**（同 IP、同窗、4 真名包 200），且形状门先于配额门判定 |
| 桶间 failover | 请求级重试：`zen_direct.chat()` 上游 429/5xx/传输失败/**匿名 403 `FreeTierError`（出口 IP 被门槛标记）** → 释放租赁 → 换桶重试（SSE 未开播，客户端零字节，可安全重试）；上限 min(桶数, 3)；**其余**请求级 4xx 不换桶；SSE 开播后不换；**兜底耗尽（或无池直连）时最终 403 `FreeTierError` 不透传**，改回 **503 + `error.code=egress_unavailable`**（`_egress_gate_answer`，出口级失败的可重试语义） | 2026-10-17 实测：单桶 429 原样透传时别的桶还有额度；hermes 桌面端的自愈重试还会把请求放大 15+ 倍；403 门槛同理须换桶而非透传；2026-10-19 06:39Z 实测：三桶同时故障兜底透传 403，hermes 判为 layer=auth、retryable=false（约 90s 后轮换已恢复全池） |
| cordon 的 IP 语义 | 429 烧穿（`free_quota_exhausted`，绑到窗口末）与 403 门槛标记（`free_tier_rejected`）是**对出口 IP 的判决**：探针观测到 agent 上报的出口 IP 变化、新 IP 落在 `expect_prefix` 内且探针就绪 → cordon **立即解除**（事件 `cordon_lifted`）；外前缀 IP 与探针未就绪不解除；transport streak cordon 保持原 deadline | 2026-10-24 实测：opc2-eth 烧穿 223.68.27.82 后 ISP 自行重拨出健康的 36.149.55.24，cordon 依旧行为绑到 UTC0 —— 健康新 IP 白白坐满全窗口，池被减员至 2 桶加剧 429 兜底透传；新 IP 若同样烧穿，会在第一个请求上重新 cordon |
| 轮换失败语义 | `POST {agent}/rotate` **传输失败 ≠ 最终失败**：命令可能已送达（或 ONT 自行重拨），照常在 deadline 内轮询 IP 变化，观测到合法新 IP 即按成功收敛；明确的 HTTP 拒绝（状态码/`ok=false`）才是最终失败 | 2026-10-24 实测：opc2-eth 硬 429 轮换的 POST 撞隧道抖动 30s 超时报 `agent unreachable`，被记 `rotation_failed`，而 ONT 实际重拨成功 —— 失败记录颠倒因果，运维被迫手动重旋 |
| agent `ready` 语义（两层防御） | agent 端 `payload.ready = egress_probe_ok is not False and 探针新鲜`：探针环每次尝试（成败皆）打单调点 `probe_at`，`PROBE_READY_GRACE_S=45s`（interval+15s）内算新鲜，环停转或最近一次探测失败 → `ready=false`；网关端 `_probe_one` 另把 `egress_probe_ok is False` 直接判 unhealthy 的第二道防线（旧 agent 缺该字段时行为不变） | 2026-10-24 实测：agent `/healthz` 的 `ready` 是硬编码 true，出口全灭（`bind_source` DHCP 漂移 + 电信腿不可达）数小时仍答 ready=true → 池以 ready 单一判据把客户端工作持续派给僵尸桶（502 燃料）；"agent 进程活着"不代表"出站可达"，须有 freshest 探测结果背书 |
| 流停顿看门狗 + 租约结局 | `STREAM_STALL_S`（默认 60s）：SSE 静默（响应头后首字节与字节间隔）超时或中途传输异常 → 记 WARNING 后**合成 `finish_reason:"length"` + `[DONE]` 优雅收尾**，异常不再穿透掐断客户端连接；关闭钩子携带结局（completed / cut / stalled / interrupted / disconnected），`stalled`/`interrupted` 按**失败**释放（`kind=stream_stalled`/`stream_interrupted`，3 连败 cordon），`cut`（上游干净截断）与客户端断连仍按 ok | 2026-10-19 实测（hermes `stream_drop`）：opc2-wifi 死隧道令流零字节静默满 `OPENCODE_ZEN_TIMEOUT_S=300` → `httpx.ReadTimeout` 穿透 `StreamRelay` → uvicorn 掐断 SSE → 客户端报 stream_drop；且 `_released` 原本恒 `ok=True`，坏桶永不累计失败、永不 cordon |
| 客户端写停滞看门狗 | `CLIENT_STALL_S`（默认 60s）：`relay_stream` 停在 `yield` 即=ASGI 写该块被背压阻塞（uvicorn `send()` 走 `flow.drain()`）——挂起超过阈值即判**客户端段已死**：WARNING `client write stalled`、结束理由 `client_stalled`，由监视任务自己取消响应任务、释放上游并写 `stream ended`；0 关闭 | 2026-10-19 实测：hermes 拿到 200 后 600s 零字节（公网段黑洞），网关侧上游仍在产块、卡在 `send()`，既无上游静默也无 TCP 断连可观测，直到 hermes 600s 自杀报 `stream_drop`；取消响应任务后 uvicorn 立即 `transport.close()`（spec 2.3 折叠任务组 → `__call__` 正常返回 → run_asgi 关连接），客户端秒级失败重试 |
| 计数→阈值轮换 | **每桶每窗口 ≥500 完成(ok)请求**闸一次轮换(ONT 重启换 IP);修复 2026-10-17:`pool.release` 的 ok 提前 return 曾跳过 observer,count 恒 0,阈值从不触发 | 2026-10-17 修好后实测 count 正常累加;**2026-10-24 修正:上游匿名日额度按成功调用计且 ≪500**（win10 十几发即烧穿,Retry-After→UTC0）,500 阈值只是兜底,主动换 IP 主要靠硬 429;且**排障探针也会烧额度**(当日全池 429 即探针自烧所致),窗口内尽量少打直刺实验 |
| 失败轮换重试 | 硬 429 触发的轮换失败后,冷却 `ROTATION_RETRY_COOLDOWN_S=900s` 可重试(防 ONT 连环重启);旧快照无 epoch 时从 last_rotation_at 解析恢复 | 2026-10-17 补全:此前一次失败(no onts/前缀误判)即锁死到窗口结束,三桶全中 |
| 桶前缀校验 | `expect_prefix` 支持逗号分隔多段 CIDR（ISP 同区多段动态分配）；不匹配 → unhealthy 跳过 | 2026-10-17 移动桶轮换后 36.149.54/24 → 223.68.27.189，单段前缀造成永久 identity_mismatch |
| updater 分级 | T1 自动（prompt 资产/UA 版本）；T2 冒烟后自动（算法常量，含全测试+真实请求）；T3 只报告 | 0.6.4 事故的教训：协议常量变化必须冒烟闸门 |
| 出口绑定 | Linux `socket.bind((source,0))`；opc2 eth 实例复用现有 uid 策略路由（`User=opc2-bucket-b`） | 与旧 opc2-eth 网关同一机制，生产已验证 |
| ONT 轮换后恢复 | ONT 重启会打掉 eth 的 DHCP 租约与策略表默认路由；恢复 = dhclient 续租 + 补默认路由 + 删 dhclient 多装的抢占路由（幂等脚本，P5 落 `post_rotate_cmd`） | 2026-10-14 真机实测踩通 |
| 桶身份校验 | 池按 `expect_prefix` 校验 agent 上报的出口 IP；不符（如 eth 掉线回落 wifi 出口）立即 cordon | 防“静默偷别的桶额度”，真机实测该回落真实存在 |

## 6. 可换 IP 的桶（ONT）— 拓扑与凭据去向

2026-10-25 现役两桶（owin10 单设备双上行）实弹验证；opc2 三桶为已下线的历史
拓扑，保留作协议参考。凭据值只存设备 JSON 与 `scripts_local/ont-credentials.md`，
本表只记型号/拓扑：

| 桶 | 出口 | ONT 管理地址 | 型号 | 驱动 `kind` | 重启验证 |
|---|---|---|---|---|---|
| `owin10-mobile` | 223.68.x / 36.149.x 移动 | 192.168.1.1（有线口 `interface=192.168.1.222` 进） | 华为 V173-50（CMCC） | `huawei`（四步 HTTP） | **已实弹**（2026-10-25）：223.68.24.82→223.68.24.122→223.68.27.206 |
| `owin10-telecom` | 106.111.x 电信 | 192.168.1.1（WiFi 经 `interface=192.168.2.235` 进） | 华为 HG8020C（电信 e8-C JSCT） | `huawei_ct`（GetRandCount token + 无状态 Cookie 头登录 + CtcApp set.cgi ResetBoard） | **已实弹**（2026-10-25）：106.111.50.124→106.111.50.165 |
| ~~opc2-eth~~（历史） | 36.149.x/223.68.x 移动 | 192.168.1.1（enp12s0 进） | 华为 V173-50（CMCC） | `huawei`（四步 HTTP） | 生产已验证（36.149.54.237→.180；2026-10-16 P5 真机再验证） |
| ~~opc2-wifi~~（历史） | 电信（宿迁段漂移：180.117.204.x / 106.110.x / 121.226.x） | 192.168.1.1（wlx 经中兴路由 192.168.2.1 进） | 华为 HG8020C（电信 e8-C JSCT） | `huawei_ct`（GetRandCount token + 无状态 Cookie 头登录 + CtcApp set.cgi ResetBoard） | **已实弹**：180.117.204.212→106.110.200.190→121.226.169.135（两轮换桶） |
| ~~cc9~~（历史） | 116.147.x/112.83.x 联通（宿迁段） | 192.168.1.1（手机 wifi 192.168.3.34 经 FTTR 从网关进） | 烽火 HG6142A-C（联通 FTTR） | `fenghuo`（get_login_user 必带 tkn=完整 UA；sha256(loginpd) 登录；logintoken cookie；ajaxmethod=reboot） | **已实弹**：112.83.208.127→116.147.31.99→116.147.30.191 |

两条驱动共性实测坑（写进 `rotate.py` 常量注释）：①电信 CtcApp 页树会间歇
404（47 字节 "The requested URL..."），需整周期重试（`CT_ARM_ATTEMPTS=2`）；
②烽火 login GET 不带 `tkn` 只回 `random_string`；③光猫对短时连续重启有
防抖（受理但不重启），轮换路径自带 900s 冷却兜底；④**Windows 侧
`onts[].interface` 必须写该上行的字面 IPv4**——Python 的 `SIOCGIFADDR`
ioctl 只存在于 Linux，写接口名在 Windows 上会被静默忽略、源地址不钉死，
两个 ONT 都在 `192.168.1.1` 时会打错光猫（`source_address()` 同时接受
接口名与字面 IPv4，测试 `test_source_address_literal_ipv4` /
`test_ont_bind_source_accepts_literal_ipv4`）。

**凭据铁律**：ONT 账号口令只写设备上的 agent 实例 JSON（Linux
`/etc/egress-agent/<实例>.json` 600、Windows `C:\egress-agent\<实例>.json`），
**不进 repo**；`egress-agent status` 输出按构造脱敏。型号/拓扑
（非密）记录在本表。

**迁移项（opc2 ONT 定时重启 cron 清理，2026-10-16 设备复核=闭环，无需动作）**：
设备侧 opc2_uname / root 两份 crontab、`/etc/cron.d/*` 与全部 systemd timers
均无 ONT 重启条目——该"每日 7 次"cron 已不存在，无需清理。P5 编排
（`ZEN_EGRESS_ROTATION`）是唯一负责重启 ONT 的组件（阈值触发 / 硬 429 触发 /
admin 手动触发，事件全落 `ZEN_EGRESS_EVENT_LOG`）。

**opc2 策略路由自愈（2026-10-17 全量修正）**：上游事件（ONT 重启 / 租约
巡检等）会吐掉规则与表默认路由，无人补则 uid 996 的 eth agent 静默回落
wifi（电信）出口、uid 1001 的 wifi agent 反向被旧栈定义污染。现状：

- `proxy_opencode-opc2b-route.service`：规则 29991（uid 996 → 表 991，经
  enp12s0 出移动）；
- `proxy_opencode-opc2a-route.service`（2026-10-17 新建）：规则 29998
  （uid 1001 → 表 410，经 wlx0c9160fb3c7c 出电信）；
- wifi agent 以专用户 `opc2-bucket-a`（uid 1001）运行
  （`egress-agent@wifi.service.d/user.conf`）；旧栈
  `proxyopencode-wired-egress.service` 已 stop+disable（它把表 410 错指
  enp12s0，曾把 wifi agent 流量带出有线口）；
- `opc2-policy-routes-keep.timer`（每 10min）已改写为幂等重放上述两个
  provision 单元，不再引用旧栈单元。

两条防火铁律：各表默认路由**只带一条默认路由**；两个 ONT LAN 同为
192.168.1.0/24，访问光猫必须按接口定向（curl `--interface` / uid 策略
路由），否则会命中对面设备。

## 7. 验收与退役判据

* 每阶段：pytest（两侧）+ ruff + mypy 全绿才推进；P1/P2/P5 有设备真机
  验证项，P3 结尾必须**本机 hermes 走 10002 全模型链路 e2e**。
* 退役（等所有者命令）：新栈连续稳定运行约定天数 → 逐项执行退役清单
  （停 zen-lb、停设备旧网关 venv、停 opc2 ONT cron、摘 9443 hermes
  alias、清残留备份），每项可单独回滚。
