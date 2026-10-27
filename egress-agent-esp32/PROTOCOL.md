# egress-agent-esp32 — Wire 协议契约（冻结 v4）

本文件是 ESP32 固件与网关侧适配器之间的**线协议权威定义**。任何一方修改
必须两端同步 bump 并更新此文件；只读方向（网关 → 固件）的字段新增需向后兼容。

## 拓扑

```
zen-gw egress 池 ──CONNECT──> gateway-adapter :10005 ──T 指令──> ESP32 :9999 控制通道
                                      │
                                      └──数据隧道（按需新建）──> ESP32 ──> 目标 host:port
```

ESP32 处于路由器 AP 隔离之后，无法被外连，因此**全部连接由 ESP32 主动发起**。

## 端口

| 端口 | 方向 | 用途 |
|---|---|---|
| 10005 | zen-gw → 适配器 | 标准 egress 桶接口：`CONNECT host:port` + `GET /healthz` |
| 9999 | ESP32 → 适配器 | 控制通道（长连）+ 数据通道（每隧道一条，短连） |

## 控制通道（ESP32 → 适配器 :9999，长连接）

首行必为 `HELLO`，其后为行式文本帧（`\n` 结尾）。

| 方向 | 帧 | 语义 |
|---|---|---|
| ESP32 → 适配器 | `HELLO <name> <lan_ip> <secret>\n` | 上线注册（v4：末段共享密钥） |
| ESP32 → 适配器 | `HB\n`（每 20s） | 心跳 |
| 适配器 → ESP32 | `PONG\n` | 心跳应答 |
| 适配器 → ESP32 | `T <id> <host>:<port>\n` | 下发隧道任务 |
| ESP32 → 适配器 | `OK <id>\n` / `ERR <id> <reason>\n` | 任务受理结果（控制通道） |
| 适配器 → ESP32 | `PING\n` / `STAT\n` / `BYE\n` | 探活 / 状态 / 优雅断开 |

`<id>` 为适配器生成的单调数字字符串。`<reason>` ∈ `target-connect` / `data-connect` / `busy` / `task-create` / `bad-port` / `bad-target`。

### v4 鉴权（2026-10-27）

适配器 :9999 必须公网可达（ESP32 从 MiFi NAT 后外连），v3 实测有公网扫描者
连入（azenv/SSH 探测）；无鉴权的 HELLO 意味着任何主机都能冒充 ESP32 抢占
桶槽并收走 CONNECT 流量。v4 在 HELLO 首行追加第 4 段共享密钥：

- 固件：`CTRL_SECRET` 常量；适配器：环境变量 `ESP32_CTRL_SECRET`；
- 适配器侧密钥为**空**时退回 v3 免鉴权（向后兼容，仅限受信内网）；
- 密钥不匹配：适配器回 `BYE\n` 并立即断开，不注册。

## 数据通道（ESP32 → 适配器 :9999，每隧道一条）

1. ESP32 先连**目标** `host:port`，成功后才连适配器发 `TREADY <id>\n`（避免适配器拿 TREADY 却等不到对端）；
2. 适配器按 `<id>` 与等待中的 CONNECT 请求配对，回 `200 Connection Established`；
3. 之后双向透传，直到任一侧关闭或空闲 20s。

**TREADY 必须走数据通道**；控制通道只承载 OK/ERR/HB 文本帧（v3 修复的 bug）。

## 超时与保活

| 参数 | 值 | 含义 |
|---|---|---|
| 心跳间隔 | 20s | ESP32 无发包时发 `HB` |
| 链路判死 | 90s | 控制通道 90s 无任何入站字节 → ESP32 主动断开重连 |
| 隧道空闲 | 20s | 数据通道无数据 → 双向关闭 |
| TREADY 等待 | 20s | 适配器等数据通道配对，超时回 502 |
| 并发额度等待 | 3s | 固件侧槽位拿不到 → 回 `ERR <id> busy` |

## 并发模型

- 固件：计数信号量限 `MAX_TUNNELS=4` 条同时隧道，每条独立 FreeRTOS 任务（栈 8KB）；
  超额拒绝并回 `ERR busy`（实测 8 并发：4 立即 / 3 排队 / 1 超时）。
- 适配器：每连接独立线程；pump 缓冲**必须每任务独立**（static 共享缓冲会跨隧道踩踏，
  v3 已修复，此约束不可回退）。

## healthz 契约

适配器对 zen-gw 暴露与 Python egress-agent 相同的 `/healthz` 形状：

```json
{"egress": "esp32-bucket", "ready": true, "egress_ip": "1.1.1.1",
 "egress_probe_ok": true, "tunnels_active": 0, "tunnels_total": 138, "uptime_s": 3917}
```

`egress_ip` 固定占位 `1.1.1.1`（真实出口 IP 是移动 CGNAT，由池侧探针另行核实）。
