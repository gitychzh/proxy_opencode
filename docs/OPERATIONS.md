# 运维手册（OPERATIONS）

> 脱敏说明：所有密钥、令牌、内网 IP 等敏感值**只存放在本地** `凭证与接入指南.md`（不入库），本文用占位符引用。

## 1. 架构总览

```
任何 OpenAI 兼容客户端（hermes / OpenAI SDK / curl）
    │  base_url = https://<公网域名>/v1   (Cloudflare Tunnel 托管)
    ▼
ECS Edge LB（zen-lb.service，:7892 内部）
    │  least-connection + 配额感知调度，429 自动熔断至 UTC 零点
    ▼
4 个桶（Tailscale 内网直连，各跑一套 proxy_opencode 网关 0.5.1）
    ├─ win10-local   :8791（本机，出口=家庭宽带）
    ├─ owin10        :8791（出口=第二宽带）
    ├─ ubuntu-26     :8791（出口=ubuntu 宽带）
    └─ phone115      :8792（Termux，出口=手机流量）
    ▼
OpenCode Zen（匿名免费层，配额按出口 IP 计）
```

- 管理面板：`https://<公网域名>/admin?key=<LB_KEY>`（实时桶健康/配额/计数）
- 公网入口由 Cloudflare Tunnel `zen-gw` 承载：ECS 上 `cloudflared-tunnel.service`，
  配置 `/etc/cloudflared/config.yml`，凭据 `/root/.cloudflared/`

## 2. ECS（SWAS 吉隆坡）

```bash
ssh root@<ECS_IP>                       # 密钥对免密（详见本地凭证文档）
systemctl status|restart zen-lb         # Edge LB
systemctl status|restart cloudflared-tunnel
vim /opt/proxy_opencode/edge_lb.env     # LB 上游清单，改完 restart zen-lb
```

- LB 配置经 EnvironmentFile 注入：`ZEN_LB_HOST/PORT/API_KEY/UPSTREAMS`
- 旧 Docker 版 LB 已删除，统一 systemd 管理

## 3. 各桶运维

| 桶 | 端口 | 守护方式 | 注意 |
|---|---|---|---|
| win10-local | 8791 | 计划任务 `zen-gw-local`：**开机+登录自启**（S4U 后台会话，脱离交互会话存活），脚本内 `:loop` 自愈循环（退出 5s 重启） | 脚本 `scripts_local/start_gw_detached.cmd`；健康检查 `GET /healthz` |
| owin10 | 8791 | 计划任务 `ProxyOpencode`（**登录触发**，重启后需人工登录一次） | sshd 在 **2222** 端口；本机公钥未授权时无法远程救援。建议登录后把任务也加开机触发 |
| ubuntu-26 | 8791 | systemd `proxy_opencode.service`（`enabled` + `Restart=always`，开机自启 ✅） | `ssh opc2_uname@<tailnet> -p 222` |
| phone115 | 8792 | Termux:Boot `~/.termux/boot/start_gw.sh`（开机自启 ✅）+ 死循环 + wake-lock | 配置 `~/repo/.env` 必须 **LF** 换行；升级版本需手机内手动清理 0.4.0 幽灵进程（Magisk `su`） |

升级桶版本：拉取 repo → `uv pip install -e .`（editable）→ 重启进程 → `/healthz` 核对版本号。
⚠️ editable 安装时若 repo 内残留 `proxy_opencode.egg-info/`，cwd 在 repo 启动的进程会读到旧版本号
（sys.path[0] 优先命中 egg-info），删除即可。

## 4. Zen 免费层协议要点（0.5.1 已内置）

1. 请求必须带 `x-session-id`（稳定 UUID）头，否则 401 "Missing API key"
2. 匿名仅 `-free` 后缀模型可用；池会轮换，`GET /v1/models` 拉全量
   （2026-09-29 实测 `big-pickle` 无 `-free` 后缀也可匿名访问）
3. 配额按出口 IP 计（约 600 请求/日/IP，UTC 零点重置）；LB 检测 429 自动熔断
4. 协议三要素（系统提示词标记 + 内置工具 schema + stream:true）由网关自动注入

## 5. Hermes 接入

- CLI 与桌面版共用 `%LOCALAPPDATA%\hermes\config.yaml`（HERMES_HOME）
- `model.default` + `provider: custom` + `base_url` + `api_key` 四件套
- **桌面版模型列表只认 models.dev 目录 + `model_aliases`**：自建网关模型必须
  在 config 顶层加 `model_aliases`（别名 → model/provider/base_url/api_key）才会显示
- 验证：`hermes -z "..."` 基础对话；`--reasoning high` 思考；工具调用建文件

## 6. 已知问题 / TODO

- [x] ~~本机网关随终端会话退出~~ → 已注册计划任务 `zen-gw-local`（AtLogOn 触发）
- [ ] **owin10 桶下线**（2026-09-29 起）：机器疑似重启后无人登录，`ProxyOpencode`
      任务（登录触发）未启动，sshd(2222) 拒绝本机公钥。**恢复步骤**：到 owin10
      本机登录一次（任务自动拉起网关），并把本机 `~/.ssh/id_ed25519.pub` 追加进
      `%USERPROFILE%\.ssh\authorized_keys`（管理员用户）
- [ ] **big-pickle 上游答案质量不稳定**（Zen/Space Bunny 好坏后端混布，简单数学题
      ~30-50% 错误率，错误响应通常缺 `reasoning_content`）。缓解：
      ① 请求带 `reasoning_effort: high` 或提示词要求逐步推理；② 需要稳定性的场景
      切 `gw-nemotron`（实测更稳）；③ 网关层无法根治，属上游问题
- [ ] **速度慢**（~22 tokens/s）：候选优化——LB 侧按延迟选桶；筛选响应更快的免费模型；
      排查 CF Tunnel 与跨洋链路开销；压缩注入的系统提示词（~7.8k prompt tokens 偏高）
- [ ] Zen 偶发上游 400/invalid request（如 `400 {"model":"big-pickle"}`）会原样
      透传给客户端（`_raw_relay_body`），属瞬时故障；LB 熔断 + 客户端重试即可

## 7. 版本历史

- **0.5.1**（2026-09-29）：适配 Zen 新门禁（x-session-id 稳定头）
- **0.5.0**：配额感知熔断、request_id 追踪、/admin 面板、双桶→四桶拓扑
- **0.4.x**：单桶原型（Docker 部署，已淘汰）
