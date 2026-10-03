# 运维手册（当前事实）

> 现状快照：2026-10-03。本文只记录当前部署状态、操作方法和仍未解决的风险。
> 版本历史与已关闭问题看 `CHANGELOG.md`；设备地址和登录方式看 `ACCESS.md`；
> 架构与安全红线看 `AGENTS.md`。历史实验文档已合并或删除，避免将旧拓扑当作现状。

## 1. 当前架构

```text
客户端
  -> https://llm.223722.xyz/v1 (Cloudflare)
  -> cloudflared 隧道 zen-gw
  -> 杭州 ECS 上的 zen-lb :7892
  -> Tailscale 上的各 proxy_opencode 桶 :8791 / :8792
  -> OpenCode Zen
```

对外模型为 `ds41f_cus`，上游 Zen 模型 ID 使用裸 ID `big-pickle`。四个桶按各自出口 IP 独立计额度；LB 做 least-connection 分发、失败重试及额度冷却。ECS/Tailscale 地址、服务及凭据位置见 `ACCESS.md`，不要把真实密钥写入仓库。

## 2. 部署与最近状态

| 节点 | 当前角色 | 最近确认状态 |
|---|---|---|
| `win10-local` / `100.121.137.118` | 用户个人 Windows 桶，8791 | 常关机属预期，不作为常驻可用性告警；除用户另行要求外不处理 |
| `owin10` / `100.109.109.108` | Windows 桶，8791 | 2026-10-02 检查为 0.6.2 |
| `ubuntu26`（`opc2_uname`）/ `100.109.57.26` | Ubuntu 桶，8791；同时运行 Hermes Gateway | 2026-10-02 检查网关为 0.6.2；Hermes 默认模型已切换到公网 LB，并于 2026-10-03 00:14 重启 gateway，systemd 显示 active/running，飞书 WebSocket 已重新连接 |
| `phone115` / `100.87.219.115` | Termux 桶，8792 | 2026-10-02 检查为 0.6.2 |
| `hangzhou-ecs` / `100.81.214.95` | 公网入口后的 LB，7892 | Tailscale `/healthz` 与 chat 正常；详细节点记录见 `ACCESS.md` |

**部署欠账**：代码库已到 0.6.3，但最近实测的四桶仍为 0.6.2，尚未完成 0.6.3 部署与旧默认管理员凭据轮换。0.6.2 缺少 0.6.3 的安全修复；在全部桶升级前，不要把桶管理端口暴露给不可信网络。升级时先检查每台启动环境已显式设置管理员/网关密钥，再同步代码、editable 安装、重启并核对 `/healthz` 版本。版本元数据从 `importlib.metadata` 读取，部署后若健康检查仍显示旧版本，检查 venv 的 editable 安装和残留 egg-info，不要手工篡改 `dist-info/METADATA` 伪造版本。

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

## 4. 协议与排障纪律

- 免费层最新复测边界：匿名请求须 `stream: true` 且包含至少两个 OpenCode 内置工具名；默认 `ZEN_TOOLS_MODE=minimal` 注入最小 schema。最近复测显示 tiny prompt / 无 system 消息也能通过，system marker 不是已验证的强制门槛。详细表述以 `AGENTS.md` 为准。
- Zen 免费额度按出口 IP 和 UTC 日期独立计量，UTC 0 点重置。`FreeUsageLimitError` 才是上游额度信号；TLS/传输错误需重试并单独诊断。
- Zen 目录已改用裸模型 ID（`big-pickle`）；`opencode/big-pickle` 旧拼写返回 401。`opencode-serve` 使用其自身模型命名空间，不要与 zen-direct 混淆。
- SSE 公网链路出现断流时，先区分 Cloudflare/网络超时、LB 到桶问题和桶到 Zen 问题。LB 日志的 `ttfb` 覆盖 LB→桶→Zen 首字节；`total` 是整流回传时间。
- 不要用真实用户密钥做批量探测；不要把请求正文、prompt 或 key 写入日志。配额探测工具使用临时 key 并在结束后吊销。

## 5. 当前网络事件：opc2 到 Cloudflare

2026-10-02/03 对比 opc2（Ubuntu）与本机 `owin10`：

- opc2 的 Cloudflare IPv6 连接失败；IPv4 对 Cloudflare 的部分 Anycast 地址丢包/建连极不稳定。MTU 实测路径约 1480，原 Wi-Fi 接口 MTU 为 2312。
- 已在 opc2 设置 Hermes `network.force_ipv4: true`、`/etc/gai.conf` IPv4 优先，并把 `wlx0c9160fb3c7c` MTU 从 2312 调到 1480；netplan 已写入 1480 并通过 `netplan generate`，运行时 MTU 也为 1480。
- 修复后，opc2 到 Cloudflare A 记录 `104.21.89.73` 的 6 次 HTTPS 健康探测有 5 次成功、1 次超时，成功请求 TCP 建连约 0.05–13 秒；对另一 A 记录 `172.67.156.243` 的 5 次强制连接均在 5 秒内失败。30 次 ICMP 对两地址分别观测到约 46.7% / 3.3% 丢包（ICMP 仅作辅助，HTTPS 结果更关键）。
- 同期 opc2 经 Tailscale 直连 ECS LB `100.81.214.95:7892` 的 `/healthz` 为 6/6 成功、每次约 38–41 ms，真实 chat 200 / 2.25 秒。因此主要故障域在 opc2 到 Cloudflare 的 ISP/Anycast 公网路径，不是 ECS LB 或四桶调度。
- 本机 `owin10` 的 Hermes CLI 是 `vgit.0ab1ffd`，本机 Hermes 真实 one-shot 返回 `owin10-pong`；本机 IPv4 公网健康检查约 1 秒，但重复测试也发生过连接超时。强制 Cloudflare `104.21.89.73` 有 3/3 成功（约 0.9–3.7 秒），`172.67.156.243` 有 0/3 成功；本机 IPv6 同样连接失败。
- **结论**：本机 Hermes 应用层配置和模型调用正常；本机到公网入口明显好于 opc2，但 Cloudflare Anycast/IPv6 仍非完全稳定。opc2 Hermes 已按用户选择保留 `https://llm.223722.xyz/v1` 公网入口，IPv4 优先配置有效，但不能承诺该公网路径稳定。已验证的稳定备用是同一 LB 的 Tailscale 地址 `http://100.81.214.95:7892/v1`；切换客户端入口前需明确确认。

本机和 opc2 的模型推理端到端验证：opc2 Hermes 返回 `pong` 与 `17*23 = 391`；本机 Hermes 返回 `owin10-pong`。本机 E2E 成功不能证明所有 Cloudflare Anycast IP 均可达；重复探测数据如上。

### 5.1 运营商差异与直连 ECS 复核（2026-10-03）

出口实测：`opc2` 与 `owin10` 是**同一出口 `36.149.54.82`（移动，江苏宿迁）**；`phone115` 出口 `117.95.231.70`（**电信**，江苏宿迁）。三机同城，故对照实为「移动 vs 电信」。

用 `curl --resolve` 强制同一 Cloudflare IP 的受控实验：

| 目标 IP | 手机（电信） | opc2（移动） | owin10（移动） |
|---|---|---|---|
| `104.21.89.73` | 5/5，~1.5 s | 5/5，0.6–6.6 s | 5/5，0.8–3.3 s |
| `172.67.156.243` | **5/5，~1.5 s** | **3/5 超时** | **4/5 超时** |

真实 chat 到 `https://llm.223722.xyz/v1`：手机（电信）5/5（4–14 s）；opc2（移动）仅 **2/5**（3 次 40 s 超时）。

**结论**：故障域是「**移动 ↔ 特定 Cloudflare Anycast IP（`172.67.156.243`）**」的对等/路由问题，不是 ECS、LB、四桶或 Cloudflare 全局。电信命中同一 IP 完全正常。因此把客户端换到电信出口可显著改善，但不可控（取决于用户 ISP）。

**直连 ECS 复核**：安全组 `sg-bp17u8pw6r3kdkgz6hes` 原开放 22/80/443/3389/8442/8443/8388 tcp + 3478/41641 udp + ICMP，**7892 原未放行**。临时放行后，`opc2` 与手机直连 `http://115.29.231.25:7892` 的 `/healthz` 均 3/3、约 **45–50 ms**，直连真实 chat 3/3（opc2 3.3–8.9 s、手机 1.3–3.0 s）。**直连比经 Cloudflare 快约 100–300 倍且无丢包。** 注意：该直连目前是**明文 HTTP**，Bearer 凭据会明文传输，正式启用前需加 TLS。临时规则仅对上述两个测试 IP 放行，描述 `temp-direct-lb-test`；是否保留/扩展/移除待定。

**访问限制**：ECS 的 SSH（root/ubuntu/ecs-user）均 publickey denied；云助手已安装（v2.2.4.1097）但服务未运行（心跳停于 2026-10-01T16:11Z），无法远程执行命令。故本轮**未能读取 `cloudflared`/`caddy` 配置**，也未能为直连配置 TLS。要完成「直连 + TLS」或「备案域名直连 443」，需先恢复 ECS 主机访问（提供 root 凭据，或修复云助手，或由控制台操作）。

### 5.2 根因定位：阿里云按域名拦截（未备案），与"无 SNI 直连 TLS"方案（2026-10-03）

§5.1 的"直连仅明文"问题已解决。完整矩阵探测（从 opc2 发起）表明拦截**不是**按端口，而是按**域名**：

| 探测 | 结果 |
|---|---|
| `Host: 115.29.231.25`（明文 7892） | 200 |
| `Host:` 任意 `*.223722.xyz`（明文 7892） | **403 `Server: Beaver`** |
| `Host: www.baidu.com`（明文 7892） | 200 |
| `openssl s_client -connect 115.29.231.25:9443`（**无 SNI**） | 握手成功 |
| 同端口 SNI = `zen`/`llm`/`223722.xyz` | **连接被 RST** |
| 同端口 SNI = `www.baidu.com` / `example.com` | 握手成功 |

**根因**：杭州 ECS 在中国大陆，阿里云 ICP 合规拦截按 `Host` 头 / TLS `SNI` 的**域名**生效，
**不限端口**；`223722.xyz` 整域未备案 → 全端口被封。`llm.223722.xyz` 能用是因为
Cloudflare 在**边缘**终止 TLS、cloudflared 由 ECS **主动出站**，阿里云从未见到入站该域名。
（此前 §5.1 判定的"移动↔特定 CF Anycast IP 路由问题"依然成立，两者是叠加的两层问题。）

**解决**：ECS caddy 用 catch-all 站点 + 自签证书（SAN 含 `IP:115.29.231.25`）在 `:9443` 服务；
客户端连 **IP 字面量** → 不发送 SNI → 不触发拦截。

- Caddyfile 关键点：Caddy 2.6 的 hostname-less 站点**不会**自动签发内部证书，必须显式
  `tls /etc/caddy/certs/gw.crt /etc/caddy/certs/gw.key`，否则握手报 `internal error (alert 80)`。
- 安全组：`9443/tcp 0.0.0.0/0` 已**永久**放行（描述 `zen-gw direct TLS entry`）；
  §5.1 的两条 `temp-direct-lb-test`（7892）与两条 `temp-9443-tls-test` 临时规则**已撤销**。
- Python 客户端（httpx/openai）默认用 **certifi**，装系统信任库不够，需 `SSL_CERT_FILE`
  或把 CA 追加进 venv 的 `certifi/cacert.pem`。

**实测**（各 20 次）：`https://115.29.231.25:9443` **20/20**（约 70–80 ms）；
`https://llm.223722.xyz` **18/20**。opc2 Hermes 默认已切到 IP 直连，one-shot 通过；
手机 chroot 内新增非默认 provider `zen-gw-direct`，默认仍为本地 `ms-glm`。

****若要正式域名入口**：只能给域名做 **ICP 备案**，或把入口迁到**非大陆节点**（港/新，免备案）。

## 6. 当前风险与待办

1. **高优先级：桶仍运行 0.6.2**。计划在具备各节点访问条件后升级到 0.6.3，确认旧默认 admin key 失效，并轮换曾暴露的凭据。不要在此手册粘贴具体凭据。
2. **公网路径**：opc2 已切 IP 直连 `https://115.29.231.25:9443/v1`（20/20）；CF 入口保留为备用（18/20）。若要长期用**域名**入口，需决策：**ICP 备案** 还是 **迁到非大陆节点**（见 §5.2）。`owin10` 仍是 CF 入口，如需同样稳定性可照 §6.5 切直连。
3. **自签证书的信任分发**是新增运维点：新增客户端需装 `ca.crt`（Linux 系统库 / Python 需 `SSL_CERT_FILE`）。证书 10 年有效，但 ECS 重装或 `/etc/caddy/certs/` 丢失需重建并重新分发。
3. **公开 `/healthz` 暴露 LB 上游拓扑、请求计数和错误摘要**。当前可公网读取，后续应评估将细节收至管理员接口或提供脱敏公开健康响应。`win10-local` 的离线是用户个人设备关机的预期状态，不在本轮处理其地址或健康告警。
4. Hermes Gateway 的 systemd user service 已于 2026-10-03 00:14 重启并显示 active/running；飞书 WebSocket 已连接。CLI 真实调用已通过，但尚未用一次真实飞书会话单独确认 Gateway 发出的模型请求，避免擅自向外发送测试消息。
5. `win10-local`（100.121.137.118）是用户个人设备且经常关机；离线为预期，不在本手册列作故障或本轮待办。

## 7. 未实施的架构想法（不是现状）

曾评估用杭州 ECS 上的单网关 + 多出口槽位替代四台异构机器各跑完整网关，另开独立入口先灰度、保留现有 `llm.223722.xyz` 回滚路径。该方案**未部署**：涉及出口独立性、SSH 转发稳定性、ECS 资源、鉴权隔离与恢复演练；不能将旧 `llm2.223722.xyz` 计划中的命令或端口当作当前部署操作。若重启该评估，先验证当前资源、访问权限与合规要求，再在不影响现网的环境做试验。
