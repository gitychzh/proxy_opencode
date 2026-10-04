# 节点访问指南（ACCESS）

> **凭据政策（2026-10-02 修订）**：本仓库是 **PUBLIC** 仓库，因此 2026-09-30
> 声明的"私有仓库明文例外"**已失效**。本文及仓库内任何文件**不得**再出现
> 真实 IP 之外的凭据（API key / AK / 令牌 / 密码 / Cookie）。
> 所有真实凭据只存放在**仓库外**的 `scripts_local/secrets.env`（已 gitignore，
> 各桶另有一份 `.env`）；本文只保留占位符与"去哪取"的指引。
> 若曾把真实凭据提交进仓库，必须**轮换**——历史记录无法靠删文件消除。
> 参见 `SECURITY.md` 与 `CONTRIBUTING.md`。

## 0. 网络底座

- **Tailscale tailnet**：四桶 + ECS 互通；SSH 全部走 Tailscale IP
- **DERP 中继**：region 901 = 自建 derper（自签证书，STUN 3478）
- **公网入口 A（Cloudflare，通用）**：`https://llm.223722.xyz/v1`（Cloudflare Tunnel，杭州 ECS 承载）
- **公网入口 B（IP 直连，移动网推荐）**：`https://115.29.231.25:9443/v1` —— **必须用 IP 而非域名**
  （域名会带 SNI 触发备案拦截），见 §7

## 1. 四桶总览

| 桶 | Tailscale IP | 网关地址 | 桶间 Key | 出口 | 系统 |
|---|---|---|---|---|---|
| win10-local（用户个人设备） | `100.121.137.118` | http://100.121.137.118:8791 | `<bucket-key>` | 家庭宽带 | Windows；经常关机属预期，不按常驻节点故障处理 |
| owin10 | `100.109.109.108` | http://100.109.109.108:8791 | `<bucket-key>` | owin10 宽带 | Windows |
| ubuntu-26（=opc2） | `100.109.57.26` | http://100.109.57.26:8791 | `<bucket-key>` | ubuntu 宽带 | Ubuntu |
| phone115 | `100.87.219.115` | http://100.87.219.115:8792 | `<bucket-key>` | 手机流量 | Android/Termux |

- 上游清单权威来源：ECS `/opt/proxy_opencode/edge_lb.env`（`ZEN_LB_UPSTREAMS`）。
  `win10-local` 的 LB healthz 检查可能显示离线；这是用户个人设备关机的预期状态，
  不应据此更换目标地址或误报全局故障。
- 四桶 `.env`/启动脚本统一：`GATEWAY_API_KEYS=<bucket-key>` +
  `ADMIN_API_KEYS=<admin-key>`（真实值见各桶 `.env` / 仓库外 `secrets.env`）

## 2. win10-local 桶（用户个人设备）

```bash
curl http://100.121.137.118:8791/healthz    # 设备在线时的远程健康检查
```

| 项 | 值 |
|---|---|
| 部署目录 | 既有设备上使用 `D:\wb_ps\proxy_opencode\repo`；关机期间不可核对版本，在线后以 `/healthz` 为准 |
| Python | `D:\wb_ps\proxy_opencode\repo\.venv\Scripts\python.exe` |
| 启动脚本 | `D:\wb_ps\proxy_opencode\scripts_local\start_gw_detached.cmd`（`HOST=0.0.0.0`，`:loop` 自愈，退出 5s 重启） |
| 守护 | 计划任务 **`zen-gw-local`**：开机+登录自启（S4U 后台会话，脱离交互会话存活） |
| 日志 | `D:\wb_ps\proxy_opencode\gateway_runtime.log` / `gateway_runtime.err.log` |
| key | 脚本内联：`GATEWAY_API_KEYS=<bucket-key>`、`ADMIN_API_KEYS=<admin-key>`（值见 `scripts_local/secrets.env`） |

```bash
# 查看计划任务状态
schtasks /query /tn zen-gw-local /v /fo list
# 手动重启桶
schtasks /end /tn zen-gw-local && schtasks /run /tn zen-gw-local
```

## 3. owin10 桶

```bash
ssh -p 2222 owin10@100.109.109.108          # 已装本机公钥，免密
```

| 项 | 值 |
|---|---|
| SSH | Windows OpenSSH，端口 **2222**；用户 `owin10`；**免密优先**（备用密码见 `secrets.env`） |
| 公钥位置 | `C:\ProgramData\ssh\administrators_authorized_keys`（管理员用户） |
| 网关目录 | `C:\Users\owin10\proxy_opencode`，启动脚本 `run.cmd`（自举 VBS 无窗 + pythonw + `:loop` 自愈；备份 `.bak_20260930_windowless`） |
| 守护 | 计划任务 `ProxyOpencodeBoot`（开机 SYSTEM 自启，**不限时** PT0S，无窗口） |
| 远端 Python | `C:\Users\Owin10\AppData\Local\Programs\Python\Python312\python.exe` |
| Hermes 配置 | `C:\Users\Owin10\AppData\Local\hermes\config.yaml`（CLI 与桌面版共用，接入示例见 §8） |

远程执行辅助：本机 `scripts_local/owin_exec.py`（paramiko，密码从 `secrets.env` 读取）。
⚠️ Git Bash 会把命令里的 `C:\` 改写成 `C://`——路径一律用 `%USERPROFILE%` 或走脚本文件。

```bash
# 计划任务（远端）
schtasks /query /tn ProxyOpencodeBoot /v /fo list
# 桶日志（远端）
type %USERPROFILE%\proxy_opencode\gateway_runtime.err.log
```

## 4. ubuntu-26 桶（= opc2 救援机）

```bash
ssh -p 2222 opc2_uname@100.109.57.26        # 本次已验证密码认证；密钥认证需另行确认
```

| 项 | 值 |
|---|---|
| SSH | Tailscale `100.109.57.26:2222`，用户 `opc2_uname`；2026-10-02/03 已验证密码认证可用。不要在仓库记录密码；端口 22 不通 |
| 守护 | systemd `proxy_opencode.service`（`enabled` + `Restart=always`，开机自启） |
| 网关目录 | `~/proxy_opencode`（桶部署） |
| Hermes 模型链路 | `~/.hermes/config.yaml` 默认 `ds41f_cus` → `https://115.29.231.25:9443/v1`（IP 直连，见 §7）；CF 入口作备用。2026-10-03 one-shot 通过 |
| 公网路径状态 | opc2 到 CF IPv6 不通、IPv4 Anycast 部分地址超时（Wi-Fi MTU 已持久设 1480）。**IP 直连 9443 实测 20/20，优于 CF 的 18/20**；Tailscale LB `http://100.81.214.95:7892/v1` 可备用 |

```bash
systemctl status|restart proxy_opencode
journalctl -u proxy_opencode -n 50
cat ~/proxy_opencode/.env        # GATEWAY_API_KEYS / ADMIN_API_KEYS 在此
```

- **网络注意**：Tailscale 的直连/中继状态随当时网络变化，先运行 `tailscale status`
  确认。远端直连不通且手机跳板已验证可用时，才考虑经手机转发：

  ```bash
  ssh -o ProxyCommand="ssh -p 2222 -o StrictHostKeyChecking=accept-new -i D:/wb_ps/proxy_opencode/phone_key/id_ed25519 -W %h:%p 100.87.219.115" -p 2222 opc2_uname@100.109.57.26
  ```

- 救援工具：`~/bin/aliyun`（AK 模式，阿里云 SWAS API 通道：RebootInstance /
  UpdateInstanceAttribute / CreateFirewallRule；AK 从环境变量读取）；
  `~/aliyun-cookies/` 有国内站 cookie 导出（会过期）
- 该机与手机 USB 相连、有 root（Magisk），ADB 授权操作可经它执行

## 5. 手机桶（phone115 · Termux）

```bash
ssh -p 2222 -i D:\wb_ps\proxy_opencode\phone_key\id_ed25519 100.87.219.115
```

| 项 | 值 |
|---|---|
| SSH | 端口 **2222**（sshd_config 里原 8022 行保留，两个端口都监听）；专用私钥 `D:\wb_ps\proxy_opencode\phone_key\id_ed25519`（**仓库外，勿入库**） |
| ADB（root） | `adb connect 100.87.219.115:43357`；root 操作 `adb shell "su -c '…'"`；本机 adbkey.pub 已写入手机信任列表；opc2 有 Magisk 模块强制 adbd TCP 43357 |
| 网关端口 | **8792**（8791 曾被 0.4.0 幽灵进程占用，已清除） |
| 守护 | `~/run_gw.sh`（死循环重启）+ `~/.termux/boot/start_gw.sh`（Termux:Boot 开机自启）+ wake-lock |
| 配置 | `~/repo/.env` —— ⚠️ **必须 LF 换行**，CRLF 会导致 .env 不生效 |
| 代码 | `~/repo/proxy_opencode/`（venv editable 安装，换代码只需覆盖包目录后重启 run_gw.sh） |

**远程拉起（2026-10-03 验证，本机 adb 直连）**：先启用 `~/.termux/termux.properties` 的
`allow-external-apps = true` 并 `am force-stop com.termux` 重启，再以 **root** 身份发 RUN_COMMAND
intent（`shell` uid 缺 `com.termux.permission.RUN_COMMAND`；`su 10254` 的 `u:r:magisk:s0` 域**无网络权限**，
会以 `could not bind on any address out of [('0.0.0.0', 8792)]` 失败）：

```bash
adb connect 100.87.219.115:43357
adb shell 'su -c "am startservice --user 0 -n com.termux/com.termux.app.RunCommandService -a com.termux.RUN_COMMAND --es com.termux.RUN_COMMAND_PATH /data/data/com.termux/files/usr/bin/bash --esa com.termux.RUN_COMMAND_ARGUMENTS -l,-c,~/.termux/boot/start_gw.sh --ez com.termux.RUN_COMMAND_BACKGROUND true --es com.termux.RUN_COMMAND_WORKDIR /data/data/com.termux/files/home"'
```

**chroot 内 hermes**：`/opt/hermes-venv/bin/hermes` 跑 one-shot 时**必须 `export HOME=/root`**，
否则读 `/.hermes/` 而非 `/root/.hermes/`，报 `No inference provider configured`。

⚠️ **进入 chroot 的正确写法（2026-10-03 修正）**：`su 0 chroot …` / `su 0 sh -c "chroot …"` 都
**不会真正 chroot**（命令仍在 Android 根下跑，`ls /` 显示 Android 根）。必须用 `su -c "chroot …"`，
并给 chroot 内的二进制**绝对路径**（`env -i` 清空 PATH 后 `wc`/`sed` 会 not found）：

```bash
adb -s 100.87.219.115:43357 shell 'su -c "chroot /data/local/chroot/ubuntu /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root /bin/bash /root/xxx.sh"'
```

已注册**非默认** provider `zen-gw-direct` → `https://115.29.231.25:9443/v1`（§7），并把自签 CA
装入系统信任库、追加进 `/opt/hermes-venv/.../certifi/cacert.pem`；默认模型仍为本地 `ms-glm`。

⚠️ 沙箱环境每次调用 adb daemon 都会重死——**connect + shell 必须写在同一条命令里**：

```bash
adb connect 100.87.219.115:43357 && adb -s 100.87.219.115:43357 shell "su -c 'netstat -tlnp | grep 8792'"
```

### 5.1 手机网络出口（联通宽带）网关：API 重启换 IP（2026-10-05 实测打通）

手机 **没插 SIM**（`gsm.sim.state=ABSENT`），它的出口 = Wi-Fi `CU_业_5G`：

```
手机(192.168.3.34) → WO-38 FTTR 从网关(192.168.3.1, 华为 V173-50 系固件)
                   → HG6142A-C FTTR 主网关(192.168.1.1, 烽火, "中国联通智能网关")
                   → 联通宽带 PPPoE（江苏宿迁；重启后 IP 会换，实测 112.83.208.192 → 112.83.208.241）
```

- 登录账号 `user`（普通用户，密码见仓库外 `D:\wb_ps\proxy_opencode\凭证与接入指南.md`）；
  注意 `192.168.1.1` 从 opc2 有线侧看到的是**另一台设备**（移动 FTTR，华为 HGU 界面），
  两台都占 192.168.1.1 但在不同 LAN——**操作手机网络网关必须从手机上发起 curl**（经 adb root）。
- **登录协议**（烽火 ajax，与华为 HGU 完全不同）：
  1. `GET /cgi-bin/ajax?ajaxmethod=get_login_user&tkn=<完整浏览器UA>` → 返回 `sessionid`。
     **坑：`tkn` 必须是完整浏览器 UA**，`tkn=M5` 之类只回 `random_string` 没有 sessionid；
  2. `POST /cgi-bin/ajax`，form 字段 `username / loginpd=sha256(password) / port=0 / sessionid / ajaxmethod=do_login`
     → 成功返回 `"login_result": 0`；
  3. 重启：`POST /cgi-bin/ajax`，body `sessionid=<sid>&ajaxmethod=reboot` → `"success": "true"`，
     约 3 分钟后 PPPoE 重拨拿到新 IP。会话按 **IP+UA** 绑定，POST 的 Referer 要指向对应 html 页。
- **3 次密码错误锁 1 分钟**；成功响应示例已验证可重复执行。
- 普通用户即可 `reboot`（`/html/resetrouter.html` 权限位 3 = 1|2）；admin 界面在首页点「管理员账户」。

**手机无人值守加固（2026-10-05 已生效，Magisk `/data/adb/service.sh` 开机自动执行）**：
`dumpsys deviceidle disable`（禁 doze）+ Termux/Tailscale 加入 deviceidle 白名单 +
`stay_on_while_plugged_in=7` + `wifi_sleep_policy=2` + `locksettings set-disabled true`（禁锁屏）。
此前小米 doze 会冻结 sshd/adbd 导致远程失联，现 adb(43357)/SSH(2222) 长期可达。

## 6. 凭证索引（**只列名与位置，不含值**）

真实值统一放在**仓库外**的 `scripts_local/secrets.env`（本机）与各桶 `.env`。
下表说明每一项**是什么、在哪用**；值请从 `secrets.env` 读取，**不要再回写进仓库**。

| 凭证 | 环境变量 / 位置 | 用途 |
|---|---|---|
| 管理员 key | `ADMIN_API_KEYS`（LB 侧 `ZEN_LB_ADMIN_KEYS`） | 用户→LB，永久，可对话 + 管理动态 key |
| 桶间 key | `GATEWAY_API_KEYS`（各桶 `.env`） | LB→四桶统一认证 |
| LB 静态 key | `ZEN_LB_API_KEY` / `ZEN_LB_API_KEYS` | 旧版兼容，仍有效 |
| 动态 key | `POST /admin/keys` 签发 | 默认 24h，keystore 在杭州 ECS `/opt/proxy_opencode/balancer/keys.json`（**勿删**），跨桶通用 |
| owin10 SSH 密码 | `secrets.env`（免密优先） | 备用登录 |
| 阿里云主账号 AK | `secrets.env`（ID + Secret） | 全权 API（RunCommand/安全组/RAM 等）；签名样例见仓库外 `scripts_local/aliyun_setup_ecs.py` |
| 阿里云控制台登录 | 见仓库外 `aliyun.md` | 控制台 cookie（会过期） |
| Cloudflare API | `secrets.env`（Global API Key + 邮箱） | 账户级操作（Tunnel/DNS/防火墙），走 `X-Auth-Email`+`X-Auth-Key` |
| 杭州 ECS root 密码 | `secrets.env`（SSH 免密优先） | 救援用 |
| 吉隆坡 SWAS root 密码 | `secrets.env`（SWAS 救援 VNC 用，SSH 免密优先） | 救援用 |
| 杭州 ECS SSH | `ssh root@115.29.231.25` | Ubuntu 24.04.5，公钥已注入 |
| 吉隆坡 SWAS SSH | `ssh -p 2222 root@47.250.130.52`（备用 22） | 密钥对 `gw-edge-2026`；Tailscale `ssh root@100.90.84.65`；**2026-10-02 起边缘角色已迁杭州，待退订** |

### 杭州新 ECS（i-bp1bzxumftqasjq6nid5 · 99元/年 · 到期 2027-10-01）

- 公网 `115.29.231.25`（安全组：22/443/80/8443/8388/8442/**9443** tcp + 3478/41641 udp；
  `9443` 为 IP 直连 TLS 入口，见 §7。7892 仅限内网/Tailscale，公网不放行）
- Ubuntu 24.04.5，2C2G，Tailscale `hangzhou-ecs` = **100.81.214.95**（四桶全 direct：owin10 42ms / phone 72ms）
- 服务：zen-lb(:7892) / cloudflared-tunnel(**必须 http2**) / caddy(:443,:80,**9443 直连入口**) / xray(:8443) / shadowsocks(:8388) / derper(:8442+STUN:3478) / tailscaled
- derper 证书指纹：`sha256-raw:883ec3b90aaa75d463c0ee4db639064a57639014aeb531e650d963a55420dbaf`（Tailscale ACL derpMap region 901 已指向）
- cloudflared DNS 坑：systemd-resolved 显式配 `223.5.5.5 + 1.1.1.1`（`/etc/systemd/resolved.conf.d/migrate.conf`），否则 argotunnel SRV 解析失败

## 7. 直连入口：为何必须用 IP 而不是域名

杭州 ECS 在中国大陆，阿里云 ICP 合规拦截按**域名**（HTTP `Host` 头 + TLS `SNI`）生效、
**不限端口**。`223722.xyz` 整域未备案 → 任意端口只要带该域名就被拦（明文 403 `Server: Beaver`、
TLS 直接 RST）；不带域名（`Host: <IP>`、无 SNI、或第三方域名）则正常。完整探测矩阵与推导见
`OPERATIONS.md` §5.2。

**绕行**：客户端连 **IP 字面量** → TLS 不发 SNI → 不触发拦截。ECS 侧 caddy 用 catch-all 站点
+ 自签证书服务该端口。

### 服务端（杭州 ECS）

```
# /etc/caddy/Caddyfile
zen.223722.xyz {
    reverse_proxy 127.0.0.1:7892
    encode gzip
}

https://:9443 {
    tls /etc/caddy/certs/gw.crt /etc/caddy/certs/gw.key
    reverse_proxy 127.0.0.1:7892
    encode gzip
}
```

- ⚠️ Caddy 2.6 的 **hostname-less 站点不会自动签发内部证书**，必须显式 `tls <crt> <key>`；
  否则该端口握手报 `internal error (alert 80)`。
- 证书 `/etc/caddy/certs/`：`ca.crt`（自签 CA，10 年）、`gw.crt`（SAN =
  `IP:115.29.231.25, IP:100.81.214.95, DNS:zen-gw.local`）。
- 安全组永久放行 `9443/tcp 0.0.0.0/0`（描述 `zen-gw direct TLS entry`）；7892 仅内网/Tailscale。

### 客户端

`base_url: https://115.29.231.25:9443/v1` —— **写 IP，不要写域名**（写域名会带 SNI 被拦）。

证书信任二选一：

1. 装 CA。Linux 系统库 `/usr/local/share/ca-certificates/` + `update-ca-certificates`；
   但 **Python 的 httpx/openai 默认用 certifi**，还需 `SSL_CERT_FILE=/path/ca-bundle.crt`
   （certifi 包 + 自签 CA 拼接），或把 CA 追加到 venv 的 `certifi/cacert.pem`。
2. 关闭校验（`verify=false` / `-k`）——不推荐。

已接入：**opc2**（Hermes 默认入口；CA 入系统库，并在 `~/.bashrc` 与 systemd drop-in
`zen-gw-ca.conf` 设 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`）；**手机 chroot**（非默认 provider
`zen-gw-direct`，默认仍为本地 `ms-glm`）。

**实测**（opc2 · 移动网 · 各 20 次）：直连 9443 **20/20**（约 70–80 ms）vs CF **18/20**；
`/v1/chat/completions` 无 key / 错 key 均 401，`/healthz` 公开。

正式**域名**入口仍需 **ICP 备案**或**迁非大陆节点**（见 `OPERATIONS.md` §5.2）。

## 8. Hermes 接入模板（各 Windows 桶通用）

配置文件：`%LOCALAPPDATA%\hermes\config.yaml`

```yaml
model:
  default:  "ds41f_cus"
  provider: "custom"
  base_url: "https://llm.223722.xyz/v1"   # 移动网可改用 IP 直连 https://115.29.231.25:9443/v1（§7）
  api_key:  "<admin-key>"          # 从 secrets.env 取，勿写死进仓库

# 桌面版模型列表只认 models.dev 目录 + 别名，必须加 model_aliases 才在 UI 可见
model_aliases:
  ds41f:
    model: ds41f_cus
    provider: custom
    base_url: "https://llm.223722.xyz/v1"
    api_key: "<admin-key>"
```

改完重启桌面版生效；CLI 可 `hermes -m ds41f` 按别名切模型。

**当前链路（2026-10-03 实测）**：`owin10` 用 CF `https://llm.223722.xyz/v1`；`opc2` 用 IP 直连
`https://115.29.231.25:9443/v1`（见 §4 与 §7）。两台 CLI 的 one-shot 均已通过；opc2 另有
Tailscale LB 入口 `http://100.81.214.95:7892/v1` 可备用。

## 9. 冒烟测试

```bash
# 公网入口（key 从 secrets.env 注入，勿写死）
curl https://llm.223722.xyz/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"model":"ds41f_cus","stream":false,"messages":[{"role":"user","content":"hi"}]}'

# 直连各桶（Tailscale 内网）
for u in 100.121.137.118:8791 100.109.109.108:8791 100.109.57.26:8791 100.87.219.115:8792; do
  curl -s "http://$u/healthz" && echo " <- $u"
done

# IP 直连入口（§7）——必须用 IP，证书用自签 CA 校验
curl --cacert ca.crt https://115.29.231.25:9443/healthz
```

## 10. 凭据轮换清单（2026-10-02 泄露后必做）

以下凭据曾以明文进入公开仓库历史，**必须全部轮换**（改密码 / 重新签发 /
吊销重建），旧值视为已泄露：

1. 阿里云主账号 AccessKey：禁用旧 AK，新建子账号 RAM 用户 + 最小权限 AK
2. Cloudflare Global API Key：在控制台 Roll，改用 Scoped API Token
3. Cloudflare Tunnel token：重建隧道凭据
4. 杭州 ECS / 吉隆坡 SWAS root 密码
5. owin10 SSH 密码
6. 网关 `ADMIN_API_KEYS` / `GATEWAY_API_KEYS` / `ZEN_LB_API_KEY`（三处全部换新值，
   同步更新各桶 `.env`、LB `edge_lb.env`、Hermes/客户端配置）
7. 已签发的动态 key：`keys.json` 全量吊销后重新签发
