# 架构总册（ARCHITECTURE）

> 本文件是 **0.7.x 新栈**的唯一权威描述：网关整体搬进 ECS 的一个 Docker
> 容器，四/五个配额桶设备只跑 `egress-agent`（设备侧出口代理）。
>
> **双轨并存（硬性）**：旧栈（ECS `zen-lb` + 各设备完整网关部署，入口
> `9443`）在新栈稳定运行前**原样保留、不做任何改动**；只有项目所有者下
> 命令后才进入退役流程（见 §7）。hermes 的旧 alias（`cusoc` → `9443`）
> 在退役前保持不动。

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
     │     ├─ catalog.py  全部免费模型 + cusoc/ds41f_cus 别名        │
     │     └─ updater.py  每日 02:00 看 opencode GitHub release      │
     └───────┬───────────┬───────────┬───────────┬──────────────────┘
        tailscale 100.x │           │           │
        ┌──────────────┴┐  ┌────────┴─┐  ┌──────┴─────┐
        ▼               ▼  ▼          ▼  ▼            ▼
   opc2:10001(wifi)  opc2:10002(eth) pad:10001  cc9:10001
   egress-agent      egress-agent     egress-agent egress-agent
   （Python, 每台上行仅一个代理进程; connECT 出去=本设备公网IP）
        │               │              │            │
        └───────────────┴──────────────┴────────────┴──▶ opencode.ai/zen/v1
```

**为什么成立**：Zen 免费层门槛是 wire 层的（descending session ID、
≥2 内置工具名、`stream:true`），全部由 ECS 网关铸造，与出口无关；额度
按出口 IP 计量，agent 的 CONNECT 出口就是各设备公网 IP，四/五桶隔离原样
保留；字节量与今天逐跳一致（ECS↔设备那段从"LB→网关 HTTP"变为"网关→
代理 CONNECT"），带宽与移动流量零新增。

## 2. 端口注册表（统一口径）

| 端口 | 归属 | 说明 |
|---|---|---|
| **10001** | 设备侧 egress-agent | 代理端口（CONNECT + /healthz + /rotate 同端口）。opc2-wifi / pad / cc9 各一个实例 |
| **10002** | 设备侧 egress-agent（opc2-eth） | 同一台设备第二上行专用；**同时是 ECS 对客户端入口**（caddy:10002 → 容器 127.0.0.1:10002） |
| **10003** | 预留 | 未来第 5/6 出口或第二服务 |
| 9443 | 旧栈入口（caddy → zen-lb:7892） | 退役前不动；hermes 旧 alias 仍在用 |
| 7892 / 8791 / 8793 / 46667 等 | 旧栈内部端口 | 退役前不动 |
| 40016 / 40066 | GLM-5.3 / GLM-5.3F 网关（ECS 现有服务） | 与新栈无关，不动 |
| 8791（本机 win10） | 旧本机网关（开发/调试） | 退役前不动 |

安全组：ECS 入站新增 10002（与 9443 同法）；设备侧 10001/10002 只听
Tailscale 100.x（systemd 绑定 0.0.0.0 但靠 tailscale 网络隔离，另加
agent 代理 basic auth 双保险）。

## 3. 组件地图（repo 布局）

| 路径 | 内容 | 阶段 |
|---|---|---|
| `proxy_opencode/egress/` | pool / counters / catalog / updater / rotate | P2-P5 逐个落地，P0 为契约桩 |
| `egress-agent/` | Python 包：config / cli / logging（P0 真实）+ proxy / egress / health / rotate（P1） | P1 |
| `egress-agent/deploy/` | systemd 模板单元、安装脚本、配置模板、opc2 双上行 uid 路由说明 | P0 |
| `egress-agent-android/` | Kotlin 原生版占位（wire 契约已冻结） | P6 |
| `deploy/ecs/` | Dockerfile/compose（P2）、caddy 10002 片段（含 LE DNS-01 说明） | P0/P2 |

## 4. 分期与状态

| 阶段 | 版本 | 内容 | 状态 |
|---|---|---|---|
| P0 | 0.7.0a1 | 冻结方案、repo 骨架、CI 双端门禁、端口注册表、caddy 片段 | **已完成**（7474c22，含既有 CI 失败修复） |
| P1 | 0.7.0 | egress-agent 数据面（CONNECT/healthz/rotate）+ 设备实测 | **已完成**（双桶出口/换 IP 真机验证通过） |
| P2 | 0.7.0 | 网关 egress pool + Docker/compose，ECS:10002 起容器；各桶出口逐一实测复核 | **已完成**（opc2-wifi / opc2-eth / cc9 三桶真机跑通；pad 待凭据延后，见下） |
| P3 | 0.7.0 | catalog 全免费模型；hermes 加新 alias（旧 alias 保留） | 未开始 |
| P4 | 0.7.1 | updater：每日 02:00 查 GitHub release（含 prerelease），分级采用 | 未开始 |
| P5 | 0.7.2 | 换 IP：计数器窗口 + 500 触发 + ONT 重启 + IP 变更验证 + 429 兜底 | **ECS 侧已完成**（2026-10-16；wifi/cc9 设备侧 API 待真机接入，见下） |
| P6 | 0.8.0 | Windows agent 转正、Android Kotlin 版、文档浓缩定稿、旧栈退役（等命令） | 未开始 |

### P5 现状（2026-10-16，ECS 侧代码完成，设备侧待真机）

- **已实现**（`proxy_opencode/egress/counters.py` + `rotate.py`，25 个新测试全绿）：
  每桶日窗 `[08:00, 08:00+1d)` Asia/Shanghai 计数（窗口自动滚动归零）；
  阈值（`ZEN_EGRESS_ROTATION`，逐桶配置，如 `opc2-eth=500`）触发**每窗口恰好
  一次**换 IP；上游 429 且 `Retry-After > 3600s`（per-IP 日耗尽实测判据）
  **绕过阈值立即换 IP**；编排流程=标记 rotating（选桶跳过、在途请求自然
  收尾）→ `POST {agent}/rotate` → 轮询 `/healthz` 等 IP **变化且落在
  `expect_prefix` 内**（同 IP 重发/腿回落都判失败，大声记录）→ 成功后清
  cordon（含配额 cordon）、计数归零重计。计数器状态 JSON 快照
  （`ZEN_EGRESS_COUNTS_PATH`，容器 `/data` 卷）跨重启存活。
- **`/admin/egresses`** 快照新增 `counters` 字段；新增
  `POST /admin/egresses/{name}/rotate`（管理员 key）= 手动触发，用于现场
  调试 wifi/cc9 光猫 API 与"IP 被重发同值"后的补一枪。
- **设备侧遗留**（agent /rotate 契约网关已就绪，等真机）：opc2-wifi
  HG8020C 重启 API 需现场复刻页面 JS 提交；cc9 光猫型号/凭据待勘查。
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

### 当前新栈公开目录（过渡态，2026-10-15）

新栈当前用 `PUBLIC_MODELS` 环境变量直接登记客户端可见名，尚未走 P3 的 catalog
代码：`cus1/deepseek-V4.1-Flash` 与 `ds41f_cus`，二者都指向上游裸 ID `big-pickle`。
P3（catalog 全免费模型）落地后改由代码统一管理，届时以 catalog 的默认值为准。

## 5. 关键决策记录（ADR 摘要）

| 决策 | 结论 | 理由 |
|---|---|---|
| agent 语言 | ubuntu/windows=**Python**；android=**Kotlin**（占位） | 稳定优先、成熟优先；现有设备全部可跑 Python（含 Android chroot） |
| 第三方依赖 | 用成熟库不自造轮子：HTTP 解析=aiohttp，ONT 重启=requests；唯独 CONNECT 双工管道自写 ~100 行 | proxy.py 评估后弃用：过重且源地址绑定能力不明确；依赖全部 pip 装本机，运行期零云端调用 |
| TLS/证书 | IP 入口沿用自签 CA（hermes 已 pin，零成本）；客户入口=域名 + LE 真证书（DNS-01，A 记录灰云直指 ECS） | 公共 CA 不签裸 IP；域名走 CF 隧道会绕海外（实测慢 10 倍），灰云直连 ECS 后是国内直连 |
| 计数器语义 | 每出口、日窗 [08:00, 08:00+1d) Asia/Shanghai（=UTC0 重置）；窗内 ≥500 触发换 IP；换完计数清零（新 IP=新额度桶） | 上游 per-IP 日配额 UTC0 重置；用户确认的语义 |
| 429 兜底 | `FreeUsageLimitError` 且 `Retry-After > 3600s` = per-IP 日耗尽 → 立即换 IP（绕过 500 阈值） | 实测两层分类：短窗软限流（等/小值）vs 日耗尽（>3600s，换 IP 立即恢复） |
| updater 分级 | T1 自动（prompt 资产/UA 版本）；T2 冒烟后自动（算法常量，含全测试+真实请求）；T3 只报告 | 0.6.4 事故的教训：协议常量变化必须冒烟闸门 |
| 出口绑定 | Linux `socket.bind((source,0))`；opc2 eth 实例复用现有 uid 策略路由（`User=opc2-bucket-b`） | 与旧 opc2-eth 网关同一机制，生产已验证 |
| ONT 轮换后恢复 | ONT 重启会打掉 eth 的 DHCP 租约与策略表默认路由；恢复 = dhclient 续租 + 补默认路由 + 删 dhclient 多装的抢占路由（幂等脚本，P5 落 `post_rotate_cmd`） | 2026-10-14 真机实测踩通 |
| 桶身份校验 | 池按 `expect_prefix` 校验 agent 上报的出口 IP；不符（如 eth 掉线回落 wifi 出口）立即 cordon | 防“静默偷别的桶额度”，真机实测该回落真实存在 |

## 6. 可换 IP 的三个桶（ONT）— 拓扑与凭据去向

| 桶 | 出口 | ONT | 型号 | 控制方式 | 重启验证 |
|---|---|---|---|---|---|
| opc2-eth | 36.149.54.x 移动 | 192.168.1.1（enp12s0 进） | 华为 V173-50 | 纯 HTTP 四步（agent `rotate` P1 实现） | 生产已验证 36.149.54.237→.180；恢复 ~80s + PPPoE ~40s |
| opc2-wifi | 117.95.231.70 电信 | 192.168.1.1（wlx 经中兴路由 192.168.2.1 进） | 华为 HG8020C | 同框架登录可行；重启 API 需现场复刻页面 JS 提交后生效（agent `/rotate` 契约已就绪） | 待设备侧接入 |
| cc9 | 2408: 联通 IPv6 | 192.168.3.1（型号/凭据待勘查） | 待定 | 大概率同华为 API（agent `/rotate` 契约已就绪；先勘查） | 待设备侧接入 |
| pad / win10 | 218.93.215.38 / 111.55.73.65 | — | — | 本期不做换 IP | — |

**凭据铁律**：ONT 账号口令只写设备上的 `/etc/egress-agent/<实例>.json`
（600），**不进 repo**；`egress-agent status` 输出按构造脱敏。型号/拓扑
（非密）记录在本表。

**迁移项**：opc2 现有"ONT 每日 7 次定时重启"cron 在新换 IP 逻辑上线
时停掉（P5），否则双重换 IP。

## 7. 验收与退役判据

* 每阶段：pytest（两侧）+ ruff + mypy 全绿才推进；P1/P2/P5 有设备真机
  验证项，P3 结尾必须**本机 hermes 走 10002 全模型链路 e2e**。
* 退役（等所有者命令）：新栈连续稳定运行约定天数 → 逐项执行退役清单
  （停 zen-lb、停设备旧网关 venv、停 opc2 ONT cron、摘 9443 hermes
  alias、清残留备份），每项可单独回滚。
