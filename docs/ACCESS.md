# 访问指南（ACCESS）

> 本仓库是 **PUBLIC**。本文只记入口、凭据**名称与去向**，**绝不记凭据值**。
> 真实值只存放在仓库外：设备侧 `/etc/egress-agent/*.json`、
> `C:\egress-agent\*.json`、ECS `deploy/ecs/.env`、`scripts_local/`（均 gitignore）。
> 端口注册表的唯一来源是 [`ARCHITECTURE.md`](ARCHITECTURE.md#2-端口注册表统一口径)。

## 1. 入口

| 用途 | 地址 | 认证 |
|---|---|---|
| 客户端（唯一可用入口） | `https://115.29.231.25:10002/v1` | `Authorization: Bearer <key>`；需先信任自签 CA |
| 健康检查 | `GET /healthz` | 无（含 `fallback` 块） |
| 模型目录 | `GET /v1/models` | 公开别名，严格 OpenAI 形状 |
| 被反代裸 ID（排障） | `GET /v1/models_cus?key=<KEY>` | Bearer 或 `?key=` |
| 桶状态 / 手动换 IP | `http://127.0.0.1:10010/admin/egresses[/{name}/rotate]` | 管理员 key，仅 loopback/Tailscale |
| 设备侧 agent | `<tailscale-ip>:10001`（owin10 第二上行 `:10002`） | CONNECT/rotate 用 Basic |

> **域名入口不可用（2026-10-25 实测）**：`llm.223722.xyz` 已解析到 Cloudflare，
> 但 `cloudflared` 隧道无 hostname ingress（只剩 404 兜底），`443`/`10002` 也不在 CF
> 代理端口白名单。恢复步骤见 [`ARCHITECTURE.md`](ARCHITECTURE.md) §10。

聊天接口只接受公开别名 `deepseek-ai/deepseek-v4.1-flash`，上游裸 ID 返回 404。

## 2. 凭据去向（仅索引）

| 类别 | 位置 | 说明 |
|---|---|---|
| 网关客户端 key | ECS `.env` 的 `GATEWAY_API_KEYS` | 逗号分隔多个；未配置则拒绝非 loopback 绑定 |
| 管理员 key | ECS `.env` 的 `ADMIN_API_KEYS` | `/admin/*`；未配置会退回公开占位符 `dev-admin-key`（严禁） |
| agent 共享 secret | ECS `.env` 的 `ZEN_EGRESS_SECRET` + 各设备 JSON 的 `listener.*` | 网关用它作 CONNECT 的 Basic 凭据；两处必须一致（注意 CRLF 会多一个 `\r`） |
| 备用链路 key | ECS `.env` 的 `ZEN_FALLBACK_API_KEY` | proxy_ms 的 `CLIENT_KEYS`，见 `/opt/proxy_ms/env.list` |
| ONT 账号口令 | 设备侧 agent 实例 JSON（600） | 仅设备本地；型号/拓扑记在 `ARCHITECTURE.md` §8 |
| ~~LB 管理 key~~ | — | 旧栈已删除（0.7.2），`ZEN_LB_API_KEY` 不再使用 |
| SSH / GitHub | 各机 `~/.ssh/` | 只授公钥，私钥不入仓（§4） |
| 临时探测 key | 运行时生成 | 用后吊销，不落盘 |

## 3. 远程命令纪律

- **Windows → ECS / 设备**：命令或脚本一律 `base64` 编码后通过 SSH 执行
  （`echo <b64> | base64 -d | python3 -`），避免特殊字符转义问题；adb 同理。
- 远程脚本避免在命令行拼接明文凭据；从 `.env` 读取而非回显。

## 4. GitHub 访问

远端 `git@github.com:gitychzh/proxy_opencode.git`（账号 `gitychzh`）。**统一走
SSH over 443**——国内 `github.com:22` 常被阻断。

```sshconfig
Host github.com
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

本机 git 远端另用 `gh-ssh` alias（同 `ssh.github.com:443`）；`http.proxy` 只影响
https 拉取。

| 机器 | 说明 |
|---|---|
| 本机（Windows 桌面） | `~/.ssh/id_ed25519` 已授权 |
| owin10（100.109.109.108:2222） | 同钥部署；工作副本 `C:\Users\Owin10\github\proxy_opencode` |

验证：`ssh -T git@github.com` 应回 `Hi gitychzh! ...`（GitHub 不给 shell，退出码
1 正常）。初始化：`git config --global user.name gitychzh`、
`user.email 47948154+gitychzh@users.noreply.github.com`。`gh` CLI 未安装，不要
依赖 `gh auth`。
