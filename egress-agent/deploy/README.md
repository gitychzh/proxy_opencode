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

```bash
sudo systemctl enable --now egress-agent@wifi egress-agent@eth
```

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
  # Group=opc2-bucket-b
  ```

  同时 `/etc/egress-agent` 与日志目录需要对该用户可读/可写
  （`install.sh --user opc2-bucket-b` 会处理好属主）。

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
