# Windows 出口桶部署（已验证路径，2026-10-25 owin10 落地）

Windows 设备只做**数据面转发**（CONNECT 隧道 + `/healthz`），网关编排全部在
ECS 容器。包 `egress_agent/` 跨平台；Windows 差异仅在两处：进程守护用
Task Scheduler（而非 systemd）、出网绑定用 `socket.bind` 源地址钉死（而非
策略路由）。

## 实测部署步骤（owin10，Windows 10 19045，Python 3.12）

1. **运行时**：`C:\Users\<user>\AppData\Local\Programs\Python\Python312\`
   （或任意 Python ≥3.11）；aiohttp 用清华镜像装：
   `python -m pip install aiohttp -i https://pypi.tuna.tsinghua.edu.cn/simple`。
2. **包目录**：`C:\egress-agent\`，内含整个 `egress_agent/` 包
   （SFTP 直传即可，无需安装）。
3. **每实例一个配置**：`C:\egress-agent\mobile.json` / `telecom.json`，
   字段与 Linux 版一致（listen、`bind_source`、healthz 等；凭据**不入仓**，
   见 `docs/ACCESS.md`）：
   - `bind_source`：源地址钉死出网网卡——Windows 强主机模型下，这是把
     出口钉到指定网卡的唯一可靠手段（双上行设备必须分实例分别绑定，
     `curl --interface` 同源验证）。owin10：有线 `192.168.1.222`（→移动
     `223.68.24.82`）、WiFi `192.168.2.235`（→电信 `106.111.50.124`）。
   - expect_prefix 允许运营商段内漂移（如 `223.68.0.0/16`）。
4. **包装脚本**：`C:\egress-agent\run_mobile.cmd` / `run_telecom.cmd`：
   ```cmd
   @echo off
   set PYTHONPATH=C:\egress-agent
   "C:\Users\Owin10\AppData\Local\Programs\Python\Python312\python.exe" ^
     -m egress_agent --config C:\egress-agent\mobile.json ^
     >> C:\egress-agent\mobile.log 2>&1
   ```
5. **开机自启（Task Scheduler，SYSTEM）**：
   ```cmd
   schtasks /Create /TN EgressAgent-mobile /SC ONSTART /RU SYSTEM /RL HIGHEST ^
     /TR "C:\egress-agent\run_mobile.cmd" /F
   ```
   双实例各一条（`EgressAgent-mobile` / `EgressAgent-telecom`）。
6. **防火墙**：放行 TCP 10001/10002 入站（仅 Tailscale 网段更佳）。
7. **验证**：ECS 上 `curl http://<tailscale-ip>:1000X/healthz`（带 Basic 凭据）
   应回 `{"ready":true,...}`，`egress_ip` 与该网卡公网出口一致；再经隧道
   CONNECT 对 `https://opencode.ai` 做数据面真测。

## 注意

- 凭据（`ZEN_EGRESS_SECRET`）只存在于设备配置文件与 ECS `.env`，绝不入仓
  也不入日志。
- 同设备多实例 = 各自独立 python 进程 + 独立 `bind_source`；Windows 无
  netns/策略路由，`bind_source` 是替代方案。
- 与既有 Windows 桶 `win10`（`deploy/windows` 旧占位路径已由本文件取代）的
  差异：win10 用 `.venv` + Startup 目录脚本；owin10 用系统 Python +
  schtasks——两条路径均已实测可用，新部署推荐 schtasks（不依赖用户登录）。
