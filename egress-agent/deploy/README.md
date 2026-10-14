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

* `wifi` 实例：默认用户运行，走主路由表（默认路由在 wlx 网卡）；
* `eth` 实例：以现有用户 `opc2-bucket-b`（uid 996，策略路由
  `uidrange 996-996 → table 991 → 192.168.1.1`）运行，并需要在配置里
  设置 `egress.bind_source` 与各 ONT 的 `interface`：

  ```bash
  sudo systemctl edit egress-agent@eth     # 加入：
  # [Service]
  # User=opc2-bucket-b
  # Group=opc2-bucket-b
  ```

  同时 `/etc/egress-agent` 与日志目录需要对该用户可读/可写
  （`install.sh --user opc2-bucket-b` 会处理好属主）。

## 凭据铁律

* ONT 用户名/口令只出现在设备配置文件中，**不进 repo、不进日志**；
* `egress-agent status` 输出按构造脱敏（无 password 字段）；
* 打包/同步配置一律先 `chmod 600`，传输用 scp/adb push 到受保护路径。
