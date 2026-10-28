# egress-agent-esp32

ESP32 桶：移动 4G MiFi（MF811）上行设备端 + 网关侧适配器。与
[`egress-agent`](../egress-agent)（Python）并列的第三种桶实现，接入
zen-gw `ZEN_EGRESS_PROXIES` 池的方式完全相同（`CONNECT` + `/healthz`）。

```
zen-gw 桶池 ──CONNECT──> gateway-adapter :10005 ──反向隧道──> ESP32 ──> MiFi 蜂窝出口
```

## 目录

| 路径 | 内容 |
|---|---|
| `firmware/` | PlatformIO 工程（ESP32-C3 SuperMini，Arduino 框架），`src/main.cpp` 单文件固件 |
| `gateway-adapter/` | `esp32_bucket_adapter.py`：跑在网关侧，把标准桶接口翻译成 ESP32 反向隧道协议 |
| `tools/` | `reboot_mifi.py`：经隧道用 Digest 认证重启 192.168.1.1 MiFi 换公网 IP |
| `deploy/` | systemd 单元（适配器常驻） |
| `PROTOCOL.md` | **线协议契约（v4）**——改协议先改这里 |

## 设计要点

- **反向连接**：ESP32 主动外连适配器（应对路由器 AP 隔离），适配器不下发任何主动连接。
- **控制/数据分离**：控制通道长连承载指令帧；每条隧道另开一条数据通道，以 `TREADY <id>` 配对。
- **HELLO 鉴权（v4）**：:9999 必须公网可达（有扫描流量），HELLO 末段携带共享密钥
  （固件 `CTRL_SECRET` 常量 / 适配器 `ESP32_CTRL_SECRET` 环境变量），不匹配即断开；
  适配器侧密钥为空退回 v3 免鉴权。**密钥值不入仓库**。
- **并发安全**：固件侧 `MAX_TUNNELS=4` 信号量 + 每任务独立栈上缓冲（见 PROTOCOL.md 并发模型）。
- **自愈**：WiFi 双网 fallback（YCHZH → YCHZHT）→ 控制通道断线自动重连 → MiFi 重启后全链路自动恢复（实测）。

## 部署（网关侧适配器）

**不要**把脚本 `cp` 到 `/opt/zen-gw/src/` 根下：那是 `git archive` 的部署快照，
每次 redeploy 整个清空，脚本随之消失、unit 抖动重启（2026-10-28 现场日志刷满
`can't open file '/opt/zen-gw/src/esp32_bucket_adapter.py'`）。让 unit 直接跑仓库
树里的路径：

```bash
# ECS 上（仓库已解包到 /opt/zen-gw/src）
cp egress-agent-esp32/deploy/esp32-bucket-adapter.service /etc/systemd/system/
printf 'ESP32_CTRL_SECRET=%s\n' '<与固件 CTRL_SECRET 一致>' > /etc/esp32-bucket-adapter.env
chmod 600 /etc/esp32-bucket-adapter.env    # 放 /etc，不要放会清空的部署树
systemctl daemon-reload && systemctl enable --now esp32-bucket-adapter
```

接入桶池（zen-gw `.env`）：`ZEN_EGRESS_PROXIES` 追加
`esp32-bucket=http://172.18.0.1:10005`（容器内经 docker bridge 网关访问宿主）。

`/healthz` 的 `egress_ip` 由控制通道的源地址实测（`egress_ip_source=control-peer`），
即 ESP32 所在 MiFi 的公网出口 IP；不再是早期写死的 `1.1.1.1` 占位。

### 隧道存活的两道 20s 门（2026-10-28 修正）

排查「同 IP 下 opencode desktop 正常、经网关全 502」时定位到两个独立的 20s：

| 位置 | 常量 | 症状 |
|---|---|---|
| 固件 `main.cpp` | `TUNNEL_IDLE_MS`（曾 20000，现 180000） | CONNECT 200 + TLS 握手成功后，**整 20.000s** 断链。慢请求（首字节 >20s）无一生还 |
| 适配器 | `ev.wait(20)` 建链等待 | ESP32 到目标 TCP 连不上/慢于 20s 时回 502 |

第一道是主因：网关自己的首字节期限是 `ZEN_TTFB_S=60`，上游 TTFB p95 实测 21.3s，
20s 的空闲上限必然误杀。修固件后需重新烧录（`pio run -t upload`，USB COM5）。

## 固件烧录

```bash
cd firmware
pio run -t upload          # upload_port 见 platformio.ini（默认 COM5）
pio device monitor         # 115200
```

WiFi 与适配器地址在 `firmware/src/main.cpp` 顶部常量区（`WIFI_LIST` / `CTRL_HOST`）。

## MiFi 换 IP

```bash
python3 tools/reboot_mifi.py    # 重启指令 200 后 60~90s 设备重拨
```

注意：移动 CGNAT 重拨**不保证**换 IP（快速连续重启常分回同一 IP）；
换 IP 验证经隧道访问 `myip.ipip.net:80`（`api.ipify.org:80` 被拦）。
