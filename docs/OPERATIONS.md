# 运维手册（当前事实）

> 现状快照：2026-10-16。本文只记录当前部署状态、操作方法和仍未解决的风险。
> 版本历史与已关闭问题看 `CHANGELOG.md`；设备地址和登录方式看 `ACCESS.md`；
> 协议事实与安全红线看 `AGENTS.md`。历史实验文档已合并或删除，避免将旧拓扑当作现状。
>
> **两栈并存**：0.7.x 新栈（网关进 ECS Docker 容器、桶设备只跑 `egress-agent`
> 出口代理，客户端入口 `https://115.29.231.25:10002/v1`）**已部署**，权威文档是
> `docs/ARCHITECTURE.md`（含端口注册表、分期状态、ONT 换 IP 拓扑）。本文 §1–§6
> 描述的旧栈（`llm.223722.xyz` + `9443` + zen-lb + 四桶完整网关）与之并存，在所有者
> 下令退役前保持原样、继续有效。

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

## 1. 当前架构（0.7.x 新栈：ECS 容器网关 + 三设备出口桶）

```text
客户端（hermes / 任意 OpenAI SDK）
  -> https://115.29.231.25:10002/v1        # caddy TLS（自签 CA zen-gw-local-ca）
  -> 127.0.0.1:10010（宿主端口映射）
  -> docker 容器 zen-gw（容器内 :10002, uvicorn）
  -> egress 池（按桶 CONNECT 到设备 agent，出口=设备各自公网 IP）
       opc2-wifi  http://100.109.57.26:10001   电信 180.117.204.0/24
       opc2-eth   http://100.109.57.26:10002   移动 36.149.54.0/24;223.68.27.0/24
       cc9        http://100.87.219.115:10001  联通 112.83.208.0/24
  -> OpenCode Zen
```

> **旧栈 9443/zen-lb/cloudflared-ingress 已于 2026-10-17 由所有者下令退役并
> 从 ECS 上彻底清除**（zen-lb / zen-glm53×3 服务停止并删除 unit；`/opt/proxy_opencode`、
> `/opt/proxy_opencode_v2`、`/opt/llmgw`、`/opt/zen-glm53` 目录删除；caddy 仅剩 `:10002`
> 一站；cloudflared 隧道清空 ingress 只剩 404 兜底）。设备侧旧栈完整网关（:879x）
> 尚未拆除，因入口链路已从 ECS 断开，处于不可达闲置状态。

0.7.x 权威描述见 `docs/ARCHITECTURE.md`。新栈对外只暴露一个掩码别名
`deepseek-ai/deepseek-v4.1-flash`（→ 唯一被反代的上游模型 `big-pickle`；
2026-10-17 起严格别名：上游裸 ID 不可请求、不可见，除隐藏端点
`/v1/models_cus` 纯裸 ID 输出）。三桶按各自出口 IP 独立计额度；容器内
least-in-flight 选择、429/5xx/传输失败**桶间 failover**、cordon 冷却、P5 每窗口
≥500 或硬 429 自动换 IP（2026-10-17 起三桶 ONT 驱动全部实弹验证，见下表）。ECS/Tailscale 地址与凭据位置见 `ACCESS.md`，
不要把真实密钥写入仓库。

## 2. 部署与最近状态（0.7.x 新栈，2026-10-17）

| 桶 | agent | 出口 IP | expect_prefix | 最近确认状态 |
|---|---|---|---|---|
| `opc2-wifi` | `100.109.57.26:10001`（egress-agent@wifi，User=opc2-bucket-a/uid1001） | `121.226.169.135` 电信 | `180.117.204.0/24;121.226.0.0/16;106.110.0.0/16` | healthy；HG8020C `huawei_ct` 驱动 2026-10-17 实弹两轮换 IP 成功 |
| `opc2-eth` | `100.109.57.26:10002`（egress-agent@eth，uid996） | `36.149.55.222` 移动 | `36.149.0.0/16;223.68.0.0/16` | healthy；P5 轮换真机验证通过；误出口修复=新建 opc2a-route(uid1001→表410@wlx) + 旧栈 wired-egress 停用 + keep-timer 改写（见 ARCHITECTURE §6） |
| `cc9` | `100.87.219.115:10001` | `116.147.30.191` 联通 | `112.83.0.0/16;116.147.0.0/16` | healthy；HG6142A-C `fenghuo` 驱动实弹受理通过（112.83.208.127→116.147.31.99→116.147.30.191）；chroot agent 由 Magisk 看门狗拉起 |
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
# 公网入口（无凭据健康检查）
curl -sS https://llm.223722.xyz/healthz

# ECS 上检查服务；设备登录方式见 ACCESS.md
systemctl status zen-lb
journalctl -u zen-lb -n 50 --no-pager

# 四桶配额复核：每桶临时签发 key、发一个真实请求、随后吊销
QUOTA_PROBE_ADMIN_KEY="$ADMIN_KEY" python scripts/probe_bucket_quota.py
```

四桶配额曾于 2026-10-02 实测全部返回 200 / QUOTA OK。重新检查配额时，应分别报告每个桶；单桶的 429、TLS EOF、DNS 失败或超时都不能推断为全局额度用尽。

项目质量门禁：

```bash
pytest tests/ -q
pytest balancer/tests/ -q
ruff check proxy_opencode tests balancer
mypy proxy_opencode
python scripts/e2e_protocols.py
```

提交前四项门禁必须全绿；`scripts/e2e_protocols.py` 使用 mock upstream 做协议端到端验证。真实 Hermes 推理是额外的环境测试，通常会消耗上游额度。

## 4. 排障纪律

免费层协议事实（`stream: true` + ≥2 个内置工具名、descending session ID、429 判读、
裸模型 ID）的**唯一权威表述在 `AGENTS.md`**，本节不重复，只列旧栈链路上的排障要点：

- SSE 公网链路出现断流时，先区分 Cloudflare/网络超时、LB 到桶问题和桶到 Zen 问题。LB 日志的 `ttfb` 覆盖 LB→桶→Zen 首字节；`total` 是整流回传时间。
- 不要用真实用户密钥做批量探测；不要把请求正文、prompt 或 key 写入日志。配额探测工具使用临时 key 并在结束后吊销。

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
自签证书（SAN 含 `IP:115.29.231.25`）在 `:9443` 服务。**部署配置、证书信任与客户端接法见
`ACCESS.md` §7**，此处不重复。

**实测**（各 20 次）：`https://115.29.231.25:9443` **20/20**（约 70–80 ms）；
`https://llm.223722.xyz` **18/20**。

**遗留**：要让 `zen.223722.xyz` 成为正式**域名**入口，只能做 **ICP 备案**，或把入口迁到
**非大陆节点**（港/新，免备案）。当前策略是「CF + IP 直连」双入口。

## 6. 当前风险与待办

1. **旧节点重新入池前先升级**：当前四个 LB 上游都已实测运行 0.6.3；旧 `win10-local` / `owin10` 已退出池但其设备可能仍运行旧版。若未来重新加入，须先升级至 0.6.3、显式配置 key 并核对 `/healthz` 版本。曾暴露的凭据仍按 `ACCESS.md` §10 轮换；不要在此手册粘贴具体凭据。
2. **公网路径**：opc2 已切 IP 直连 `https://115.29.231.25:9443/v1`（20/20）；CF 入口保留为备用（18/20）。若要长期用**域名**入口，需决策 **ICP 备案** 还是 **迁非大陆节点**（见 §5.2）。`owin10` 仍是 CF 入口，如需同样稳定性可照 `ACCESS.md` §7 切直连。
3. **自签证书的信任分发**是新增运维点：新增客户端需装 `ca.crt`（Linux 系统库；Python 还需 `SSL_CERT_FILE`）。证书 10 年有效，但 ECS 重装或 `/etc/caddy/certs/` 丢失需重建并重新分发。
4. **公开 `/healthz` 暴露 LB 上游拓扑、请求计数和错误摘要**。当前可公网读取，后续应评估将细节收至管理员接口或提供脱敏公开健康响应。
5. Hermes Gateway（opc2）此前因飞书 WebSocket 正常关闭被 `lark_oapi` 抛出而退出，2026-10-03 已重启恢复；飞书会话尚未单独验证 Gateway 发出的模型请求，避免擅自向外发送测试消息。
6. **ECS 部署的 `balancer/lb.py` 与仓库 `main` 已经分叉**（2026-10-11 发现），两边互缺，合流前不要相互覆盖：

   | 能力 | 仓库 `main` | ECS 部署版 |
   | --- | --- | --- |
   | 模型目录 `ZEN_LB_MODELS="id:group"` + 上游第 4 字段分组（当前生产配置依赖） | ❌ 缺（只认 3 字段，**部署上去会因解析失败起不来**） | ✅ 有 |
   | `TRUST_ENV`：出站 httpx 不跟随 ambient `HTTP_PROXY` | ✅ 有 | ❌ 缺 |
   | `ClientGone`：客户端断连不计入桶故障 | ✅ 有 | ❌ 缺 |
   | `ZEN_LB_ADMIN_KEYS` 默认值为空（fail-closed） | ✅ 有 | ❌ **默认回落到硬编码 key**（0.6.3 已修的问题回归） |

   影响评估：ECS 当前未设置任何 proxy 环境变量，第 2 行只是潜在风险；第 3 行最坏效果是
   连续 3 次客户端断连（如连按 Ctrl+C 中断流式回答）把健康桶标为 unhealthy，但健康检查
   每 20s 会将其恢复（`mark_health` 成功时清零 `consecutive_failures`），**属瞬时抖动**。
   第 4 行是真实凭据回归，需留意轮换（§ `ACCESS.md` §10）。
   **合流方向**：以仓库 `main` 为基底（保留全部加固），把「模型目录 + 分组」移植上去，
   跑通 `balancer/tests/` 后再部署；变更后立即用 `/healthz` 与一次真实 chat 验证，并保留
   `edge_lb.env` 与 `lb.py` 备份以便回滚。

## 7. 单网关 + 多出口槽位：已落地为 0.7.x 新栈

曾评估用杭州 ECS 上的单网关 + 多出口槽位替代四台异构机器各跑完整网关。该方案
**已落地**为 0.7.x 新栈：网关进 ECS Docker 容器，桶设备只跑 `egress-agent`，客户端
入口 `https://115.29.231.25:10002/v1`。权威描述（端口注册表、分期状态、ADR、ONT
换 IP 拓扑）见 `docs/ARCHITECTURE.md`，客户端接法与证书见 `ACCESS.md` §0.1 / §7。

旧栈（`llm.223722.xyz` + `9443` + zen-lb + 四桶完整网关）与新栈并存，在所有者下令
退役前继续作为回滚路径保留。历史文档里记的 `llm2.223722.xyz` 计划命令与端口**不是**
新栈的操作步骤，勿混用。
