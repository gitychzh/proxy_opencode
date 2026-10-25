# ECS 部署（0.7.x 单容器）

生产部署的唯一入口。现状与操作见 [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md)，
拓扑与端口见 [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)。

## 文件

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 网关镜像（`python:3.12-slim` + 内置 bun + `pip install .`） |
| `compose.yml` | 容器编排：发布到宿主 `127.0.0.1:10010`，只读根文件系统 + `/data` 卷 |
| `env.example` | 环境变量模板；复制为 `.env` 填真实值（`.env` 已 gitignore） |
| `Caddyfile.snippet` | 客户端入口 `:10002`（IP 自签 + 域名 LE 两方案）→ 反代 `127.0.0.1:10010` |

## 构建与运行

```bash
cd deploy/ecs
cp env.example .env          # 填 GATEWAY_API_KEYS / ADMIN_API_KEYS / ZEN_EGRESS_*
docker compose build
docker compose up -d && docker compose logs -f zen-gw
docker compose ps            # 期望 healthy（HEALTHCHECK 打 /healthz）
```

**构建前置**：ECS 需能访问一个 pip 源（默认 `pypi.tuna.tsinghua.edu.cn`）。若
docker.io 拉不到 `python:3.12-slim`，先配 `/etc/docker/daemon.json` 的
`registry-mirrors` 或导入内网基础镜像，再重试。运行期不依赖任何云服务。

## 验证

```bash
curl -s http://127.0.0.1:10010/healthz | python3 -m json.tool
curl -s http://127.0.0.1:10010/admin/egresses \
  -H "Authorization: Bearer <key>" | python3 -m json.tool
# 每桶应 healthy=true，egress_ip 落在其 expect_prefix 内
```

**手动换一次 IP**（仅限配置了光猫的桶；自动触发走 `ZEN_EGRESS_ROTATION` 阈值 /
硬 429，共用同一编排）：

```bash
curl -s -X POST http://127.0.0.1:10010/admin/egresses/owin10-mobile/rotate \
  -H "Authorization: Bearer <key>"
# 返回 {"started": true}；成败看 counters.last_rotation_*。
# 光猫约 80s 恢复 + 30-40s PPPoE 回拨。
```

## 事件日志

`ZEN_EGRESS_EVENT_LOG=/data/egress_events.jsonl`，一行一 JSON（UTC），换 IP 全
时间线含前后 IP：`rotation_requested → started(ip_from) → succeeded(ip_from,
ip_to, elapsed_s)` / `rotation_failed(detail)`；另有 `cordon`、`cordon_lifted`
（出口 IP 变化解除配额/门槛 cordon）、`egress_ip_observed`、`window_rollover`、
`free_catalog_refreshed`、`fallback_*`。5 MB×2 轮转，只含桶名/IP/计数/时间，
无凭据与消息内容。事件清单见 [`docs/EGRESS_OBSERVABILITY.md`](../../docs/EGRESS_OBSERVABILITY.md)。

```bash
tail -f deploy/ecs/data/egress_events.jsonl
docker compose exec zen-gw tail -f /data/egress_events.jsonl
```

## 被反代模型浏览（`/v1/models_cus`）

唯一展示上游裸 ID 的端点，浏览器可直接打开（IP 入口需先信任自签 CA / 或用域名
入口）：

```
https://115.29.231.25:10002/v1/models_cus?key=<网关或管理员KEY>
```

鉴权接受 `?key=` 或 Bearer。**不要**给该端点再造普通名字的别名（会被扫描 agent
捕获）。裸 ID 仅排障可见：聊天接口一律 404，客户端用公开别名；标准 `/v1/models`
永远只列公开别名（严格 OpenAI 形状）。

## 入口（caddy）

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F-%H%M)
# 1) 把 Caddyfile.snippet 追加进 /etc/caddy/Caddyfile
# 2) sudo caddy validate --config /etc/caddy/Caddyfile
# 3) sudo systemctl reload caddy        # 失败则 restart 回滚
# 4) 阿里云安全组放行入站 TCP 10002
```

TLS 两方案：

1. **IP + 自签 CA（默认）**：复用 `/etc/caddy/certs/gw.crt` / `gw.key`（hermes 已
   pin 该 CA，`ssl_ca_cert` 不变）。
2. **域名 + LE 真证书（客户零安装）**：`llm.223722.xyz` 的 A 记录**灰云**（仅
   DNS，不走 CF 代理——CF 海外绕行实测慢 10 倍）直指 ECS IP，caddy 用 Cloudflare
   DNS-01 签证书。CF API Token（Zone.DNS:edit）写入 caddy 服务环境
   （`systemctl edit caddy` → `Environment=CF_API_TOKEN=...`），不进仓库。

## 回滚

```bash
cd deploy/ecs && docker compose down     # 容器退出
# 删除 Caddyfile 里追加的 :10002 site 块并 systemctl reload caddy
```
