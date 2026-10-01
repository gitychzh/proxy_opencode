# 节点访问指南（ACCESS · 明文版）

> **明文声明**：家庭局域网环境，安全不设防。经仓库所有者确认（2026-09-30），
> 所有 IP、key、密码、令牌**明文入库**，本仓库为私有仓库。
> 本文与本地 `凭证与接入指南.md` 内容同步，冲突时以本地文档为准并回写本文。

## 0. 网络底座

- **Tailscale tailnet**：四桶 + ECS 互通；SSH 全部走 Tailscale IP
- **DERP 中继**：region 901 = 自建 `gw-kl`（47.250.130.52:8442，自签证书，STUN 3478）
- **公网入口**：`https://llm.223722.xyz/v1`（Cloudflare Tunnel `zen-gw`，ECS 承载）

## 1. 四桶总览

| 桶 | Tailscale IP | 网关地址 | 桶间 Key | 出口 | 系统 |
|---|---|---|---|---|---|
| win10-local（本机） | `100.121.137.118` | http://100.121.137.118:8791 | `api_local22372222` | 家庭宽带 | Windows |
| owin10 | `100.109.109.108` | http://100.109.109.108:8791 | `api_local22372222` | owin10 宽带 | Windows |
| ubuntu-26（=opc2） | `100.109.57.26` | http://100.109.57.26:8791 | `api_local22372222` | ubuntu 宽带 | Ubuntu |
| phone115 | `100.87.219.115` | http://100.87.219.115:8792 | `api_local22372222` | 手机流量 | Android/Termux |

- 上游清单权威来源：ECS `/opt/proxy_opencode/edge_lb.env`（`ZEN_LB_UPSTREAMS`）
- 四桶 `.env`/启动脚本统一：`GATEWAY_API_KEYS=api_local22372222` + `ADMIN_API_KEYS=api_ychzh22372222`

## 2. 本机 win10 桶（win10-local）

```bash
curl http://127.0.0.1:8791/healthz          # 健康检查（版本号在此确认）
```

| 项 | 值 |
|---|---|
| 仓库 | `D:\wb_ps\proxy_opencode\repo`（v0.6.2） |
| Python | `D:\wb_ps\proxy_opencode\repo\.venv\Scripts\python.exe` |
| 启动脚本 | `D:\wb_ps\proxy_opencode\scripts_local\start_gw_detached.cmd`（`HOST=0.0.0.0`，`:loop` 自愈，退出 5s 重启） |
| 守护 | 计划任务 **`zen-gw-local`**：开机+登录自启（S4U 后台会话，脱离交互会话存活） |
| 日志 | `D:\wb_ps\proxy_opencode\gateway_runtime.log` / `gateway_runtime.err.log` |
| key | 脚本内联：`GATEWAY_API_KEYS=api_local22372222`、`ADMIN_API_KEYS=api_ychzh22372222` |

```bash
# 查看计划任务状态
schtasks /query /tn zen-gw-local /v /fo list
# 手动重启桶
schtasks /end /tn zen-gw-local && schtasks /run /tn zen-gw-local
```

## 3. owin10 桶

```bash
ssh -p 2222 owin10@100.109.109.108          # 已装本机公钥，免密
ssh -p 2222 owin10@100.109.109.108          # 备用密码：223722
```

| 项 | 值 |
|---|---|
| SSH | Windows OpenSSH，端口 **2222**；用户 `owin10`；密码 `223722`（免密优先） |
| 公钥位置 | `C:\ProgramData\ssh\administrators_authorized_keys`（管理员用户） |
| 网关目录 | `C:\Users\owin10\proxy_opencode`，启动脚本 `run.cmd`（自举 VBS 无窗 + pythonw + `:loop` 自愈；备份 `.bak_20260930_windowless`） |
| 守护 | 计划任务 `ProxyOpencodeBoot`（开机 SYSTEM 自启，**不限时** PT0S，无窗口） |
| 远端 Python | `C:\Users\Owin10\AppData\Local\Programs\Python\Python312\python.exe` |
| Hermes 配置 | `C:\Users\Owin10\AppData\Local\hermes\config.yaml`（CLI 与桌面版共用，接入示例见 §7） |

远程执行辅助：本机 `scripts_local/owin_exec.py`（paramiko，密码内置）。
⚠️ Git Bash 会把命令里的 `C:\` 改写成 `C://`——路径一律用 `%USERPROFILE%` 或走脚本文件。

```bash
# 计划任务（远端）
schtasks /query /tn ProxyOpencodeBoot /v /fo list
# 桶日志（远端）
type %USERPROFILE%\proxy_opencode\gateway_runtime.err.log
```

## 4. ubuntu-26 桶（= opc2 救援机）

```bash
ssh -p 2222 opc2_uname@100.109.57.26        # 免密（本机 ~/.ssh/config 别名即 `ssh opc2_uname`）
```

| 项 | 值 |
|---|---|
| SSH | 端口 **2222**（2026-09-30 统一），用户 `opc2_uname`，密钥 `~/.ssh/id_ed25519` |
| 守护 | systemd `proxy_opencode.service`（`enabled` + `Restart=always`，开机自启） |
| 网关目录 | `~/proxy_opencode`（桶部署） |

```bash
systemctl status|restart proxy_opencode
journalctl -u proxy_opencode -n 50
cat ~/proxy_opencode/.env        # GATEWAY_API_KEYS / ADMIN_API_KEYS 在此
```

- **网络注意**：该机 Tailscale 只有 relay 路径（DERP region 901）；DERP 半死时 SSH 握手超时，
  改用**手机跳板**（手机↔opc2 同局域网走 direct）：

  ```bash
  ssh -o ProxyCommand="ssh -p 2222 -o StrictHostKeyChecking=accept-new -i D:/wb_ps/proxy_opencode/phone_key/id_ed25519 -W %h:%p 100.87.219.115" -p 2222 opc2_uname@100.109.57.26
  ```

- 救援工具：`~/bin/aliyun`（AK 模式，阿里云 SWAS API 通道：RebootInstance /
  UpdateInstanceAttribute / CreateFirewallRule）；`~/aliyun-cookies/` 有国内站 cookie 导出（会过期）
- 该机与手机 USB 相连、有 root（Magisk），ADB 授权操作可经它执行

## 5. 手机桶（phone115 · Termux）

```bash
ssh -p 2222 -i D:\wb_ps\proxy_opencode\phone_key\id_ed25519 100.87.219.115
```

| 项 | 值 |
|---|---|
| SSH | 端口 **2222**（sshd_config 里原 8022 行保留，两个端口都监听）；专用私钥 `D:\wb_ps\proxy_opencode\phone_key\id_ed25519` |
| ADB（root） | `adb connect 100.87.219.115:43357`；root 操作 `adb shell "su -c '…'"`；本机 adbkey.pub 已写入手机信任列表；opc2 有 Magisk 模块强制 adbd TCP 43357 |
| 网关端口 | **8792**（8791 曾被 0.4.0 幽灵进程占用，已清除） |
| 守护 | `~/run_gw.sh`（死循环重启）+ `~/.termux/boot/start_gw.sh`（Termux:Boot 开机自启）+ wake-lock |
| 配置 | `~/repo/.env` —— ⚠️ **必须 LF 换行**，CRLF 会导致 .env 不生效 |
| 代码 | `~/repo/proxy_opencode/`（venv editable 安装，换代码只需覆盖包目录后重启 run_gw.sh） |

⚠️ 沙箱环境每次调用 adb daemon 都会重死——**connect + shell 必须写在同一条命令里**：

```bash
adb connect 100.87.219.115:43357 && adb -s 100.87.219.115:43357 shell "su -c 'netstat -tlnp | grep 8792'"
```

## 6. 凭证速查（明文）

| 凭证 | 值 | 用途 |
|---|---|---|
| 管理员 key | `api_ychzh22372222` | 用户→LB，永久，可对话+管理动态 key |
| 桶间 key | `api_local22372222` | LB→四桶统一认证 |
| LB 静态 key（兼容） | `gw-lb-rYPuNWGjPMo1WB_kqENRC5sh` | 旧版已并入 LB 静态 key，仍有效 |
| 动态 key | `POST /admin/keys` 签发 | 默认 24h，keystore 在杭州 ECS `/opt/proxy_opencode/balancer/keys.json`（**勿删**），跨桶通用 |
| owin10 SSH 密码 | `223722` | 备用（免密优先） |
| 阿里云主账号 AK | ID `LTAI5t9LNM3tj8y6USLdiq11` / Secret `4n2a3LBwKhiJN1nr8s2t3OZWfwjLsp` | 全权 API（RunCommand/安全组/RAM 等），签名样例见仓库外 `scripts_local/aliyun_setup_ecs.py` |
| 阿里云登录 | nick6730986768（UID 1983070104853754） | 控制台 cookie 明文在仓库外 `aliyun.md` |
| Cloudflare | Global API Key `cfk_If85oo2i2sJCxbJT66encnVvwQUdBlmgJHIKLO9u9bd3191c` + 邮箱 `93921526@163.com` | 账户级全权，走 `X-Auth-Email`+`X-Auth-Key` 头（GitHub SSO 登录，旧 cfut_ token 已失效删除） |
| 杭州 ECS root 密码 | `HzEcs2026Rk9mQx` | SSH 免密优先（本机 id_ed25519 已注入） |
| 吉隆坡 SWAS root 密码 | `GwFix2026xX` | SWAS 救援 VNC 用（SSH 免密优先） |
| 杭州 ECS SSH | `ssh root@115.29.231.25` | Ubuntu 24.04.5，公钥已注入 |
| 吉隆坡 SWAS SSH | `ssh -p 2222 root@47.250.130.52`（备用 22） | 密钥对 `gw-edge-2026`；Tailscale `ssh root@100.90.84.65`；**2026-10-02 起边缘角色已迁杭州，待退订** |

### 杭州新 ECS（i-bp1bzxumftqasjq6nid5 · 99元/年 · 到期 2027-10-01）

- 公网 `115.29.231.25`（安全组：22/443/80/8443/8388/8442 tcp + 3478/41641 udp）
- Ubuntu 24.04.5，2C2G，Tailscale `hangzhou-ecs` = **100.81.214.95**（四桶全 direct：owin10 42ms / phone 72ms）
- 服务：zen-lb(:7892) / cloudflared-tunnel(zen-gw, **必须 http2**) / caddy(:443,:80) / xray(:8443) / shadowsocks(:8388) / derper(:8442+STUN:3478) / tailscaled
- derper 证书指纹：`sha256-raw:883ec3b90aaa75d463c0ee4db639064a57639014aeb531e650d963a55420dbaf`（Tailscale ACL derpMap region 901 已指向）
- cloudflared DNS 坑：systemd-resolved 显式配 `223.5.5.5 + 1.1.1.1`（`/etc/systemd/resolved.conf.d/migrate.conf`），否则 argotunnel SRV 解析失败

## 7. Hermes 接入模板（各 Windows 桶通用）

配置文件：`%LOCALAPPDATA%\hermes\config.yaml`

```yaml
model:
  default:  "ds41f_cus"
  provider: "custom"
  base_url: "https://llm.223722.xyz/v1"
  api_key:  "api_ychzh22372222"

# 桌面版模型列表只认 models.dev 目录 + 别名，必须加 model_aliases 才在 UI 可见
model_aliases:
  ds41f:
    model: ds41f_cus
    provider: custom
    base_url: "https://llm.223722.xyz/v1"
    api_key: "api_ychzh22372222"
```

改完重启桌面版生效；CLI 可 `hermes -m ds41f` 按别名切模型。

**直连提速（2026-09-30 起，两台 Windows 已采用）**：本机 hermes 把上面两处
base_url 换成 `http://127.0.0.1:8791/v1`（owin10 同理），绕开公网链路省 ~2s/请求。
代价：失去 LB 跨桶 failover（本桶配额打满即 429）；换回公网域名即恢复。
api_key 用管理员 key（各桶通用），无需改动。

## 8. 冒烟测试

```bash
# 公网入口
curl https://llm.223722.xyz/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer api_ychzh22372222" \
  -d '{"model":"ds41f_cus","stream":false,"messages":[{"role":"user","content":"hi"}]}'

# 直连各桶（Tailscale 内网）
for u in 100.121.137.118:8791 100.109.109.108:8791 100.109.57.26:8791 100.87.219.115:8792; do
  curl -s "http://$u/healthz" && echo " <- $u"
done
```
