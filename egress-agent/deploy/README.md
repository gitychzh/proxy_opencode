# egress-agent 部署模板

模板文件，供 `install.sh` 使用。**真实配置只落在设备上**
（`/etc/egress-agent/<实例名>.json`，权限 600），仓库内永远只有占位符。

## systemd 实例单元

`egress-agent@.service` 是模板单元，按实例取名：

| 实例 | 配置文件 | 监听 | 出口 |
|---|---|---|---|
| `egress-agent@wifi` | `/etc/egress-agent/wifi.json` | 10001 | wifi 上行 |
| `egress-agent@eth`  | `/etc/egress-agent/eth.json`  | 10002 | eth 上行（opc2 专用） |
| `egress-agent@pad`  | `/etc/egress-agent/pad.json`  | 10001 | pad 上行 |
| `egress-agent@cc9`  | `/etc/egress-agent/cc9.json`  | 10001 | cc9 上行 |

> 安装：`sudo ./install.sh <instance> [--user <u>] [--python <3.12 解释器>] [--pip-index <url>]`。
> 它把 agent 装进 `/opt/egress-agent/venv`（不依赖系统 python 版本，也不受
> `ProtectHome=true` 影响），安装模板单元，并建好 `/etc/egress-agent` 与
> `/var/log/egress-agent`。装完手动放真实配置再 `enable --now`。

```bash
sudo ./install.sh wifi --python /opt/python3.12/bin/python3.12
sudo ./install.sh eth  --user opc2-bucket-b --python /opt/python3.12/bin/python3.12
sudo systemctl enable --now egress-agent@wifi
sudo cp config.example.json /etc/egress-agent/wifi.json   # 填真实值
sudo systemctl enable --now egress-agent@eth
sudo cp config.example.json /etc/egress-agent/eth.json     # 填真实值
sudo systemctl enable --now egress-agent@wifi egress-agent@eth
```

真实配置在 `/etc/egress-agent/<实例>.json`（权限 600，含 ONT 口令与 CONNECT
认证）；`listener.username/password` 与网关的 `ZEN_EGRESS_SECRET` 保持一致
（网关用该 secret 作 CONNECT 的 Basic 凭据）。

## opc2 双上行（uid 策略路由）

opc2 的两台光猫共占 192.168.1.1，出口靠系统策略路由区分（与旧网关
`proxy_opencode-opc2b.service` 同一机制）：

* `wifi` 实例：默认用户运行 + `egress.bind_source=192.168.2.138`
  （源 IP 绑定即可，wifi 是默认路由腿）；
* `eth` 实例：**不要**设 `bind_source`——默认路由走 wifi，源 IP 绑定会直接
  发不出包。以现有用户 `opc2-bucket-b` 运行（uid 996，策略路由
  `uidrange 996-996 → table 991 → 192.168.1.1`）：

  ```bash
  sudo systemctl edit egress-agent@eth     # 加入：
  # [Service]
  # User=opc2-bucket-b
  ```

  `opc2-bucket-b` 无同名组（gid=1000），systemd 会用其主组；**不要**写
  `Group=opc2-bucket-b`（组不存在会启动失败）。`install.sh --user opc2-bucket-b`
  会把 `/etc/egress-agent` 的属主改为该用户。

## opc2 策略路由自愈（2026-10-16 设备落地）

两个 provision 单元（`proxy_opencode-opc2b-route.service` = 表 991/rule 29991
uid 996；`proxyopencode-wired-egress.service` = 表 410/rules 1040+1050
uid 997）是 RemainingAfterExit boot oneshot：上游事件（ONT 重启、租约巡检等）
把规则/表默认路由吐掉后无人补，uid 996 的 eth agent 会**静默回落 wifi（电信）
出口**——唯一可见症状是池侧 `expect_prefix` 校验的 identity mismatch。固化
自愈（设备上已安装）：

```
/etc/systemd/system/opc2-policy-routes-keep.service   # oneshot：restart 两个 provision 单元
/etc/systemd/system/opc2-policy-routes-keep.timer     # OnBootSec=2min + OnUnitActiveSec=10min
systemctl enable --now opc2-policy-routes-keep.timer
```

restart 语义保证幂等（ExecStop 删规则/flush 410 → ExecStart `route
replace` + `rule add`），实测无规则堆积。ONT（V173-50）重启或 P5 触发的 eth
轮换之后，路由恢复窗口 ≤10min；enp12s0 租约由 systemd-networkd
（`05-workbuddy-enp12s0-dhcp.network`）自动续，"dhclient 四步恢复"仅保留为
历史应急路径（见下节）。首次 P5 真实轮换仍应人工盯一次全流程（P5 验收项）。

## Android + chroot（cc9：无 systemd，用 Magisk service.d）

Android 设备没有 systemd，上面的 `install.sh` / `egress-agent@.service` 不适用。
cc9 的做法：chroot Ubuntu（`/data/local/chroot/ubuntu`，aarch64，自带
python3.12）里手装 `/opt/egress-agent/venv`，然后用
`android-chroot-watchdog.sh`（Magisk `service.d`）守护进程。

* 手装：`python3 -m venv /opt/egress-agent/venv && /opt/egress-agent/venv/bin/pip install <agent 源>`，
  配置放 `/etc/egress-agent/<实例>.json`（600）。chroot 里 pypi 可直连
  （cc9 走 IPv6）。
* 守护：把 `android-chroot-watchdog.sh` 顶上读 `INSTANCE` / `PORT` / `CHROOT`
  改成实际值，拷到 `/data/adb/service.d/egress-agent-watchdog.sh`（755）；
  开机自动拉起，3 次健康失败即重启（真机杀死 agent 后 ~90s 自愈已验证）。
  想立即起可 `setsid sh /data/adb/service.d/egress-agent-watchdog.sh </dev/null >/dev/null 2>&1 &`。
* **IPv6-only 出口**（cc9 联通只有 IPv6）：`bind_source` 留空，出站靠
  asyncio 按 getaddrinfo 顺序 IPv4 快速失败→回落 IPv6；网关侧
  `expect_prefix` 用 IPv6 `/64`。
* 凭据只进 chroot 内 `/etc/egress-agent/*.json`。

## ONT 轮换后的恢复（P5 前置，真机实测结论）

ONT 重启会把 eth 腿的 DHCP 租约与 table 991/410 的默认路由一起打掉，
且 dhclient 续租会装一条 metric 0 的抢占默认路由（整机流量翻到 eth）。
恢复脚本必须做全四步（幂等，P5 将以 `post_rotate_cmd` 配置）：

```bash
sudo dhclient -r enp12s0 && sudo dhclient enp12s0
sudo ip route del default via 192.168.1.1 dev enp12s0          # 删 dhclient 多装的
ip route show table 991 | grep -q '^default' || sudo ip route add default via 192.168.1.1 dev enp12s0 src 192.168.1.222 table 991
ip route show table 410 | grep -q '^default' || sudo ip route add default via 192.168.1.1 dev enp12s0 table 410
```

## 凭据铁律

* ONT 用户名/口令只出现在设备配置文件中，**不进 repo、不进日志**；
* `egress-agent status` 输出按构造脱敏（无 password 字段）；
* 打包/同步配置一律先 `chmod 600`，传输用 scp/adb push 到受保护路径。
