# ECS 新栈部署（0.7.x）

容器与入口配置。**应用时机**：P2（网关 egress pool + 容器落地）。旧栈
（caddy:9443 → zen-lb:7892、设备旧网关）在本目录任何配置生效前保持原样，
hermes 旧 alias 不动。

## 文件

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 网关镜像（`python:3.12-slim` + `pip install .`，运行期零云端调用） |
| `compose.yml` | 容器编排：发布到 `127.0.0.1:10002`，只读根文件系统 + `/data` 卷 |
| `env.example` | 环境变量模板（含出口池）；复制为 `.env` 填真实值（`.env` 已 gitignore） |
| `Caddyfile.snippet` | 新客户端入口 `:10002`（IP 自签 + 域名 LE 两方案）+ 容器反代 |
| `README.md` | 本文件 |

## 容器构建与运行（P2）

```bash
cd deploy/ecs
cp env.example .env          # 填 GATEWAY_API_KEYS / ADMIN_API_KEYS / ZEN_EGRESS_*
# 构建（默认清华 TUNA pip 源；镜像源不可用时换 --build-arg PIP_INDEX_URL=...）
docker compose build
docker compose up -d
docker compose logs -f zen-gw
docker compose ps            # 期望 healthy（HEALTHCHECK 打 /healthz）
```

**构建前置**：ECS 需能访问一个 pip 源（默认 `pypi.tuna.tsinghua.edu.cn`）。
若 docker.io 拉不到 `python:3.12-slim` 基础镜像，先配置 registry 镜像加速
（`/etc/docker/daemon.json` 的 `registry-mirrors`）或用内网导入的基础镜像，
再 `docker compose build`。构建在容器内完成，运行期不依赖任何云服务。

**验证出口池**（容器起来后，用 ADMIN key）：

```bash
curl -s http://127.0.0.1:10002/admin/egresses \
  -H "Authorization: Bearer $ADMIN_API_KEYS" | python3 -m json.tool
# 每桶应 healthy=true、egress_ip 落在其 expect_prefix 内。
```

**手动换一次 IP**（P5，仅限配置了光猫的桶；自动触发走
`ZEN_EGRESS_ROTATION` 阈值/硬 429，二者共用同一编排）：

```bash
curl -s -X POST http://127.0.0.1:10002/admin/egresses/opc2-eth/rotate \
  -H "Authorization: Bearer $ADMIN_API_KEYS"
# 返回 {"started": true}；再查 /admin/egresses 的 counters.last_rotation_*。
# 重启光猫约 80s 恢复 + 30-40s PPPoE 回拨，最后成败以 last_rotation 为准。
```

**换 IP 的可观测日志**（`ZEN_EGRESS_EVENT_LOG=/data/egress_events.jsonl`）：
一行一 JSON 事件（UTC 时间戳），换 IP 全时间线含**前后 IP 对照**：
`rotation_requested → rotation_started(ip_from) → rotation_succeeded
(ip_from, ip_to, elapsed_s)` 或 `rotation_failed(detail)`；另有
`cordon`（429 配额/连续失败）、`egress_ip_observed`（探测到的 IP 变化）、
`window_rollover`（08:00 日窗全部归零）、`free_catalog_refreshed`。
查看：

```bash
tail -f deploy/ecs/data/egress_events.jsonl
# 或进容器：docker compose exec zen-gw tail -f /data/egress_events.jsonl
```

大小轮转 5MB×2（同目录 `.1`/`.2`）。事件只含桶名/IP/计数/时间，无凭据与
消息内容（红线）。

**免费模型浏览（P3，/v1/models1）**：全部免费模型裸 ID 的唯一展示端点，
浏览器直接打开（IP 入口需先信任自签 CA / 或用域名入口）：

```
https://115.29.231.25:10002/v1/models1?key=<网关或管理员KEY>
```

鉴权接受 `?key=` 或 Bearer；不要给该端点再造普通名字的别名（models/model
等会被扫描 agent 捕获）。这些裸 ID 在 `/v1/chat/completions` 里原样透传；
标准 `/v1/models` 永远只列公开别名。

## 应用入口（caddy）

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F-%H%M)
# 1) 把 Caddyfile.snippet 的内容追加进 /etc/caddy/Caddyfile
# 2) sudo caddy validate --config /etc/caddy/Caddyfile
# 3) sudo systemctl reload caddy        # 失败即 systemctl restart caddy 回滚
# 4) 阿里云安全组放行入站 TCP 10002（与 9443 同法）
# 5) （可选，客户零安装）配置 Cloudflare DNS-01，见下
```

## 两种 TLS 方案

1. **IP + 自签 CA（默认）**：复用现有 `/etc/caddy/certs/gw.crt` /
   `gw.key`（hermes 早已 pin 该 CA，`ssl_ca_cert` 不变）。
2. **域名 + LE 真证书（客户零安装）**：`llm.223722.xyz` 的 A 记录**灰云**
   （仅 DNS，不走 CF 代理——CF 海外绕行实测慢 10 倍）直指 ECS IP，
   caddy 用 Cloudflare DNS-01 签真证书。前置条件：CF API Token
   （Zone.DNS:edit）写入 caddy 服务环境（`systemctl edit caddy` →
   `Environment=CF_API_TOKEN=...`，token 只进系统环境，不进仓库）。
   DNS-01 的稳定性验证是 P2 的验收项之一。

## 回滚

```bash
cd deploy/ecs && docker compose down          # 容器退出，旧栈零影响
# 删除 Caddyfile 里追加的 :10002 site 块并 systemctl reload caddy
```

## 日志红线

容器日志经 json-file 驱动（8MB×3 轮转）。网关绝不记录消息内容、prompt 或
任何 key（见 SECURITY.md）；`/admin/egresses` 只暴露出口名 / IP / 计数。
