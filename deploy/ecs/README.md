# ECS 部署（0.8.x 双容器）

生产部署的唯一入口。现状与操作见 [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md)，
拓扑与端口见 [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)。

```
客户端 ──10002──> zen-gw-edge（公网入口，只透传 + 选路）
                     ├ primary  ──> zen-gw:10002（网关本体，compose 网络内）
                     └ fallback ──> 172.17.0.1:10003（proxy_ms，ModelScope
                                                   deepseek-v4.1-flash-1017）
```

## 文件

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 镜像（`python:3.12-slim` + 内置 bun + `pip install .`）；**两个容器共用这一个镜像** |
| `compose.yml` | 编排：`edge` 发布宿主 `10002`，`zen-gw` 只 `expose`；只读根文件系统 + `/data` 卷 |
| `env.example` | 环境变量模板；复制为 `.env` 填真实值（`.env` 已 gitignore） |
| `Caddyfile.snippet` | **历史遗留**：TLS 入口方案（IP 自签 + 域名 LE）。2026-10-28 起不再使用，客户端直连 10002 |

## 构建与运行

```bash
cd deploy/ecs
cp env.example .env          # 填 GATEWAY_API_KEYS / ADMIN_API_KEYS / ZEN_EGRESS_* / EDGE_ADMIN_KEY
docker compose build
docker compose up -d && docker compose logs -f zen-gw-edge
docker compose ps            # 期望两个容器都 healthy（HEALTHCHECK 打 /healthz）
```

**构建前置**：ECS 需能访问一个 pip 源（默认 `pypi.tuna.tsinghua.edu.cn`）。若
docker.io 拉不到 `python:3.12-slim`，先配 `/etc/docker/daemon.json` 的
`registry-mirrors` 或导入内网基础镜像，再重试。运行期不依赖任何云服务。

## 日常更新：只重建网关，入口不动

这就是拆两个容器的全部意义 —— 重建 `zen-gw` 期间 `edge` 把请求转给 `proxy_ms`：

```bash
cd /opt/zen-gw/src && docker build -q -f deploy/ecs/Dockerfile -t zen-gw:0.8 -t zen-gw:<ver> .
cd deploy/ecs && docker compose up -d --no-build zen-gw    # 只动这一个容器
```

要连"模型 ID 都不换"，就先 drain 再重建（`EDGE_ADMIN_KEY` 在 `.env` 里）：

```bash
K=$(grep '^EDGE_ADMIN_KEY=' .env | cut -d= -f2-)
curl -s -H "X-Admin-Key: $K" -X POST -d '{"mode":"fallback"}' http://127.0.0.1:10002/edge/mode
# 重建 ...
curl -s -H "X-Admin-Key: $K" -X POST -d '{"mode":"auto"}'     http://127.0.0.1:10002/edge/mode
```

## 验证

```bash
# 1) edge 自己：role/primary.alive/fallback.configured 三者缺一就说明 env 没接对
curl -s http://127.0.0.1:10002/healthz | python3 -m json.tool

# 2) 主链正常时 x-edge-route 应为 primary
curl -s -D- -o /dev/null http://127.0.0.1:10002/v1/chat/completions \
  -H "Authorization: Bearer <GW_KEY>" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-ai/deepseek-v4.1-flash","stream":false,"max_tokens":16,
       "messages":[{"role":"user","content":"say ok"}]}' | grep -i x-edge

# 3) 降级演练（确认重建期间用户不掉线）：停网关 → 同一请求仍 200 且 route=fallback
docker compose stop zen-gw
curl -s -D- -o /dev/null ...同上... | grep -i 'x-edge\|HTTP/'
docker compose start zen-gw   # 10s 后 healthz 的 primary.alive 应回到 true

# 4) 桶状态（走 edge 透传到网关的 admin）
curl -s http://127.0.0.1:10002/admin/egresses \
  -H "Authorization: Bearer <ADMIN_KEY>" | python3 -m json.tool
```

**手动换一次 IP**（仅限配置了光猫的桶；自动触发走 `ZEN_EGRESS_ROTATION` 阈值 /
硬 429，共用同一编排）：

```bash
curl -s -X POST http://127.0.0.1:10002/admin/egresses/owin10-mobile/rotate \
  -H "Authorization: Bearer <ADMIN_KEY>"
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

edge 自身的降级记录走容器 stdout（`docker compose logs -f zen-gw-edge`）：
`edge: primary unavailable (...)` / `edge: primary cannot serve (...), switching to
fallback` / `edge: mode auto -> fallback (manual)`。

## 被反代模型浏览（`/v1/models_cus`）

唯一展示上游裸 ID 的端点，浏览器可直接打开（明文入口，无需证书）：

```
http://115.29.231.25:10002/v1/models_cus?key=<网关或管理员KEY>
```

鉴权接受 `?key=` 或 Bearer。**不要**给该端点再造普通名字的别名（会被扫描 agent
捕获）。裸 ID 仅排障可见：聊天接口一律 404，客户端用公开别名；标准 `/v1/models`
永远只列公开别名（严格 OpenAI 形状）。

## 入口

**当前（0.8.0 起）**：`zen-gw-edge` 容器绑定公网 `10002`，明文 HTTP + API key；
`zen-gw` 只在 compose 网络内 `expose 10002`。没有 caddy、没有证书，客户端零安装。
安全组放行入站 TCP 10002 即可。

**0.7.x（since-retired）**：网关容器自己发布 `10002` —— 重建即断连，这正是 0.8.0
拆容器要解决的问题。

**历史方案（已下线，保留以备恢复）**：caddy 监听 `:10002` 做 TLS，反代
`127.0.0.1:10010`。配置见 `Caddyfile.snippet`；恢复步骤：

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F-%H%M)
# 1) 把 Caddyfile.snippet 追加进 /etc/caddy/Caddyfile
# 2) sudo caddy validate --config /etc/caddy/Caddyfile
# 3) sudo systemctl reload caddy        # 失败则 restart 回滚
# 4) compose.yml 的 edge.ports 改为 "127.0.0.1:10010:10002"，docker compose up -d
```

TLS 两方案（历史）：

1. **IP + 自签 CA**：复用 `/etc/caddy/certs/gw.crt` / `gw.key`（hermes 已
   pin 该 CA，`ssl_ca_cert` 不变）。
2. **域名 + LE 真证书（客户零安装）** —— **2026-10-29 已废弃**，域名入口整体
   删除（DNS 记录、隧道、cloudflared 二进制全清）。当年方案：`llm.223722.xyz`
   的 A 记录**灰云**（仅 DNS，不走 CF 代理——CF 海外绕行实测慢 10 倍）直指 ECS
   IP，caddy 用 Cloudflare DNS-01 签证书。重建需先 ICP 备案。

## 回滚

```bash
cd deploy/ecs && docker compose down     # 两个容器一起退出（10002 彻底关闭）
# 只回滚网关、保住入口：
cd deploy/ecs && docker compose up -d --no-build zen-gw   # 用旧镜像重跑网关
```
