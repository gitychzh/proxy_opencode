# ECS 新栈部署（0.7.x）

容器与入口配置。**应用时机**：P2（网关 egress pool 落地后）。旧栈
（caddy:9443 → zen-lb:7892、设备旧网关）在本目录任何配置生效前保持原样。

## 文件

| 文件 | 用途 |
|---|---|
| `Caddyfile.snippet` | 新客户端入口 `:10002`（两种 TLS 方案）+ 容器反代 |
| `compose.yml` | P2 落地（网关镜像；构建/运行期零云端依赖） |
| `README.md` | 本文件 |

## 应用步骤（P2 时执行，先备份后改）

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F-%H%M)
sudo python3 -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]))" /dev/null 2>/dev/null || true
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

删除追加的 site 块并 `systemctl reload caddy` 即回到纯 9443 旧入口；
容器 `docker compose down` 不影响任何旧组件。
