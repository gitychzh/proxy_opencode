# 运维手册（OPERATIONS）

> **凭证说明（2026-09-30 更新）**：经仓库所有者确认按明文方式管理——所有 IP、key、
> 密码已明文写入 [`docs/ACCESS.md`](ACCESS.md)（家庭局域网环境，私有仓库，安全不设防）。
> 本文保留脱敏叙述作为运维流程手册，具体值以 ACCESS.md 为准。

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

### 2.1 请求级耗时日志（0.6.1 起，ECS 端可观测）

```bash
ssh root@<ECS_IP> tail -f /opt/proxy_opencode/balancer/logs/lb.log
```

每条转发成功日志：

```
rid=<id> lb POST /v1/chat/completions -> win10-local status=200 attempt=1 ttfb=4247ms total=5468ms
```

读法（2026-09-30 三路径 ablation 标定：直打上游 TTFT ~2.3-4.2s，桶内开销 <0.5s，
公网链路固定 ~0.7s）：

| 字段 | 覆盖段 | 异常解读 |
|---|---|---|
| `ttfb` | LB→桶→上游首字节（含桶代码 + Zen TTFT） | 持续走高 = 上游慢或桶慢；对照直打上游基线 |
| `total` | 整个响应流回传完 | ttfb 平稳而 total 增长 = 大响应体（正常） |
| `attempt` | 重试次数 | >1 = 有桶 429/502 被跳过（看 WARNING 行详情） |

429/502/超时日志同样带 `ttfb=`/`elapsed=`，配额熔断行有 `cordoned for quota`。

## 2. ECS（杭州活动机，2026-10-02 迁移完成）

> 吉隆坡 SWAS（47.250.130.52，到期 2027-09-25）自 2026-10-02 起退出边缘角色，
> cloudflared 已 stop+disable；caddy/xray/ss/derper 空转待退订。

```bash
ssh root@115.29.231.25                  # 杭州新机（公钥免密）；详见本地凭证文档
systemctl status|restart zen-lb         # Edge LB :7892
systemctl status|restart cloudflared-tunnel
vim /opt/proxy_opencode/edge_lb.env     # LB 上游清单，改完 restart zen-lb
```

- LB 配置经 EnvironmentFile 注入：`ZEN_LB_HOST/PORT/API_KEY/UPSTREAMS`
- 旧 Docker 版 LB 已删除，统一 systemd 管理
- 迁移坑（2026-10-02 实战）：
  - **cloudflared 必须 `protocol: http2`**（/etc/cloudflared/config.yml）：QUIC 出境绕
    LAX 丢包严重（models 成功率 7/10），http2 后 12/12 全通
  - **systemd-resolved 显式配 DNS**（`/etc/systemd/resolved.conf.d/migrate.conf`，
    223.5.5.5 + 1.1.1.1）：阿里默认解析不了 `_v2-origintunneld._tcp.argotunnel.com` SRV
  - caddy 报 217/USER = 缺 caddy 系统用户，`useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy`
  - 安全组：22/443/80/8443/8388/8442 tcp + 3478/41641 udp（AuthorizeSecurityGroup API）
  - zen-lb keys.json 实际路径：`/opt/proxy_opencode/balancer/keys.json`

## 3. 各桶运维

| 桶 | 端口 | 守护方式 | 注意 |
|---|---|---|---|
| win10-local | 8791 | 计划任务 `zen-gw-local`：**开机+登录自启**（S4U 后台会话，脱离交互会话存活），脚本内 `:loop` 自愈循环（退出 5s 重启） | 脚本 `scripts_local/start_gw_detached.cmd`；健康检查 `GET /healthz` |
| owin10 | 8791 | 计划任务 `ProxyOpencodeBoot`（**开机 SYSTEM 自启** ✅）+ `ProxyOpencode`（登录触发）+ `run.cmd` 内置 `:loop` 自愈循环 | SSH：`ssh -p 2222 owin10@<tailnet>`（已装本机公钥，免密 ✅；Windows OpenSSH，管理员公钥在 `C:\ProgramData\ssh\administrators_authorized_keys`） |
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

## 5. 接入各客户端（0.6.0 起）

- **对外唯一模型：`ds41f_cus`**（DeepSeek V4.1 Flash）。客户端请求任意模型名
  都透明路由到上游；响应（含流式 SSE）的模型字段一律改写为 `ds41f_cus`，
  上游真实模型名不出网关（`MASK_MODELS=false` 可关闭）。
- **三种协议接口**：
  - `POST /v1/chat/completions`——OpenAI 格式（hermes / 任意 SDK），Bearer 认证
  - `POST /v1/responses`——OpenAI Responses 格式（**codex CLI** 直连，
    `wire_api = "responses"`），Bearer 认证
  - `POST /v1/messages`——Anthropic Messages 格式（**claude code** 直连，
    `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`），`x-api-key` 或 Bearer
- **API key 有效期**（0.6.0 起）：
  - 管理员 key：`api_ychzh22372222`（`ADMIN_API_KEYS`，**永久**，可管理 key）
  - 存量 `GATEWAY_API_KEYS` 静态 key：永久（四桶配置向后兼容）
  - 新签发 key：默认 **24 小时**；`POST /admin/keys` 可自定义 TTL（0=永久）
  - 管理：`POST/GET/DELETE /admin/keys`（仅管理员 key；列表脱敏）
  - 存储文件：`keys.json`（KEY_STORE_PATH，已 gitignore；**不要删桶上的
    keys.json**，删了已签发 key 全部失效）
- Hermes 桌面版仍需 `model_aliases` 才会显示自建模型（别名指向 `ds41f_cus`）。

## 6. Hermes 接入（chat_completions）

- CLI 与桌面版共用 `%LOCALAPPDATA%\hermes\config.yaml`（HERMES_HOME）
- `model.default` + `provider: custom` + `base_url` + `api_key` 四件套
- **桌面版模型列表只认 models.dev 目录 + `model_aliases`**：自建网关模型必须
  在 config 顶层加 `model_aliases`（别名 → model/provider/base_url/api_key）才会显示
- 验证：`hermes -z "..."` 基础对话；`--reasoning high` 思考；工具调用建文件

## 7. 已知问题 / TODO

- [x] ~~本机网关随终端会话退出~~ → 已注册计划任务 `zen-gw-local`（AtLogOn 触发）
- [x] ~~owin10 桶下线~~ → 2026-09-29 晚恢复：拉起网关 + 建 `ProxyOpencodeBoot`（ONSTART/SYSTEM）
      开机自启任务 + `run.cmd` 加自愈循环 + 本机公钥装入 `administrators_authorized_keys`（免密 SSH ✅）
- [ ] **big-pickle 上游答案质量不稳定**（Zen/Space Bunny 好坏后端混布，简单数学题
      ~30-50% 错误率，错误响应通常缺 `reasoning_content`）。缓解：
      ① 请求带 `reasoning_effort: high` 或提示词要求逐步推理；② 需要稳定性的场景
      切 `gw-nemotron`（实测更稳）；③ 网关层无法根治，属上游问题
- [x] ~~速度慢——网关侧可做的部分~~ → **0.6.1 已完成**（2026-09-30）：①拆除每 key
      60 RPM 限速（agent 并发扇出不再被自建层惩罚）；②活体 ablation 实测 Zen 门禁
      只查工具 schema（≥2 个即过），`ZEN_MARKER_MODE=bridge` + `ZEN_TOOLS_MODE=minimal`
      把每请求 prefill 从 ~7.8k 降到 ~2.3k tokens（-70%）；③两台 Windows 的 hermes
      切本桶直连（省公网链路 ~2s/请求）。
- [ ] **剩余慢因子全在上游**（网关无法再压）：Zen TTFT ~2-4s（上游排队+启动）、
      生成 ~22 tokens/s（模型速度）。候选：筛选响应更快的免费模型
- [ ] Zen 偶发上游 400/invalid request（如 `400 {"model":"big-pickle"}`）会原样
      透传给客户端（`_raw_relay_body`），属瞬时故障；LB 熔断 + 客户端重试即可

## 8. 版本历史

- **边缘迁移**（2026-10-02）：KL SWAS → 杭州活动机（99元/年）。7 服务全量迁移并
  逐个调试：zen-lb/cloudflared/caddy/xray/shadowsocks/derper(重签证书+derpMap
  就地更新)/tailscale（auth key 经 GitHub SSO 生成，节点 hangzhou-ecs=100.81.214.95，
  四桶全 direct 42-72ms）。域名 llm./zen. 均已切杭州，E2E 公网推理 200。
  阿里云主账号 AK/CF Global API Key 明文见 ACCESS.md §6。
- **0.6.2**（2026-09-30）：系统性缺陷排查与工程化重构。修复 Responses 流式
  `output_index` 冲突与缺函数参数增量事件、Anthropic 空 assistant 轮次导致
  上游 400、SSE 事件体缺 `type`、keystore 异形 JSON 致**启动崩溃**、LB 全桶
  饱和误返 502、admin 错误码 200→400/404、日志白名单丢字段、`PUBLIC_MODELS`
  解析丢条目；新增 `routes/_pipeline.py` 收敛三条协议路由重复代码；去除
  `app` 导入期副作用；CI 补齐 ruff + mypy 门禁（此前从未真正执行）。
- **0.6.1**（2026-09-30）：移除每 key 限流；Zen 门禁指纹重校准（bridge/minimal，
  prefill ~7.8k → ~1k tokens）
- **0.6.0**（2026-09-30）：对外唯一模型 ds41f_cus（掩码）；key 有效期 + /admin/keys；
  Responses API（codex）与 Anthropic API（claude code）；auth/ formats/ 模块化细分
- **0.5.1**（2026-09-29）：适配 Zen 新门禁（x-session-id 稳定头）
- **0.5.0**：配额感知熔断、request_id 追踪、/admin 面板、双桶→四桶拓扑
- **0.4.x**：单桶原型（Docker 部署，已淘汰）

## 9. 全链路验证记录（2026-09-30）

在 owin10 上以 hermes 与 claude code 为客户端，覆盖四桶直连 + LB + 公网入口
的实测记录。可复现命令见 `scripts/e2e_hermes.md`。

### 9.1 拓扑连通性（全部通过）

| 目标 | 地址 | healthz | 版本 |
| --- | --- | --- | --- |
| win10-local | `100.121.137.118:8791` | 200（本机 5ms） | 0.6.1 |
| owin10 | `100.109.109.108:8791` | 200 | 0.6.1 |
| ubuntu26 | `100.109.57.26:8791` | 200 | 0.6.1 |
| phone115 | `100.87.219.115:8792` | 200 | 0.6.1 |
| ECS LB | `100.90.84.65:7892` / `47.250.130.52:7892` | 200 | — |
| 公网入口 | `https://llm.223722.xyz` | 200 | — |

四桶直连非流式 / 流式对话均 200；`/v1/responses`（codex 协议）与
`/v1/messages`（Anthropic 协议）经公网入口均返回正确内容。

### 9.2 延迟基准（N=5 非流式，max_tokens=200）

| 路径 | TTFB 中位 | 总耗时 中位 | 总耗时 min/max |
| --- | --- | --- | --- |
| ubuntu26 直连 | 2946ms | **2948ms** | 2441/4863 |
| owin10 直连 | 3184ms | 3197ms | 2401/7527 |
| phone115 直连 | 3042ms | 4061ms | 2354/5805 |
| LB 公网入口 | 4554ms | **5082ms** | 4444/5885 |

结论：**直连桶比公网入口快约 1.5–2.1s/请求**（与 ACCESS.md 的 ~2s 一致）。
owin10 的 hermes / claude code 目前都指向公网入口，尚未启用直连提速。
注意 phone115（Termux）出现 1/6 连接失败，稳定性弱于其他三桶。

### 9.3 客户端实测（owin10）

| 用例 | 耗时 | 结果 |
| --- | --- | --- |
| hermes 普通问答 | 20.0s | ✅ 正确 |
| hermes 推理（`--reasoning high`） | 18.2s | ✅ 正确（Reasoning 面板可见） |
| hermes 工具（`-t terminal -t file --yolo`） | 25s | ✅ 落盘 `e2e_probe.txt`=hello，6 次工具调用 |
| claude code 普通问答 | 18.8s | ✅ 正确 |
| claude code 工具（Write/Read） | 77.3s | ✅ 落盘 `c_e2e.txt`=hi |

⚠️ claude code 对 `ds41f_cus` 报 `[claude-code:unrecognized_model]`：不在其模型
目录内，auto-compact 按 200k 上下文估算。功能不受影响；如需消除告警，可用
`modelOverrides`/`behavesAs` 映射，或设 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`。

### 9.4 实测确认的两个部署缺陷（已在 0.6.2 修复，需重新部署桶与 LB）

1. **SSE 泄漏 `delta.name`**：Zen 回填上游模型名 `Space Bunny`，旧掩码只改
   `model`，故随流式响应泄漏。已在 `sse_mask.py` 修复。
2. **LB 熔断过激**：win10-local 桶在实测中 **可正常服务**（多次 200），却因
   早先一次 429 被 LB 熔断到 UTC 零点（快照显示 `quota_exhausted=true,
   reset=747min`，而直连该桶持续返回 200），静默损失 1/4 容量。已改为冷却
   窗口（默认 900s 自动重探）。

> 上述两项均在**部署版 0.6.1** 上实测复现；修复位于 0.6.2，**需要把四桶与
> ECS LB 重新部署**后才会生效。

### 9.5 三个客户端实际使用的协议（抓包实证）

用本机 8877 透明抓包代理（stdlib，转发到真实网关）实测：

| 客户端 | 协议 | 端点 | 备注 |
| --- | --- | --- | --- |
| hermes | **chat_completions**（主）+ responses（辅助） | `/v1/chat/completions` 2 次；`/v1/responses` 1 次 | 主请求 40KB；另有 `/v1/models`、`/api/tags` 等探测 |
| codex / codex++ | **responses** | `/v1/responses` | `config.toml` 里 `wire_api = "responses"` |
| claude code | **anthropic messages** | `/v1/messages?beta=true` | 同时发 stream=True 与 False 两版 |

**结论：三种格式都在被真实使用，都必须保留。** hermes 走 chat（还顺带打了
一次 responses），codex 走 responses，claude code 走 anthropic。

### 9.6 提示词构成与瘦身（本轮重点）

输入 token 的构成（抓包实测）：

| 客户端 | 工具定义 | 系统提示词 | 消息 | 合计 |
| --- | --- | --- | --- | --- |
| claude code | **15,068 tok / 23 个（79%）** | 1,451 tok | 2,416 tok | ~19.1k |
| codex | **6,786 tok / 15 个** | 42 tok | ~1,600 tok | ~8.5k |
| hermes | **5,989 tok / 14 个** | 4,042 tok | ~13 tok | ~10.1k |

**工具定义是绝对大头**，故优化都落在工具上：

1. **网关注入（全客户端，省 ~1,900 tok/请求）**：门禁只认「≥2 个内置工具名」，
   schema 内容不校验 → 注入从 7,872 字符降到 246 字符。
2. **claude code（省 ~9,500 tok/请求，-50%）**：`permissions.deny` 摘除 11 个
   与编码无关的工具（DesignSync/SendMessage/Workflow/ScheduleWakeup/Cron*/
   Worktree*/ReportFindings/ListAgents）——Claude Code 会把它们**从请求里
   移除**。请求体 76,027 → 37,740 字符。
3. **codex（待定，潜在 ~5,500 tok）**：`~/.codex/config.toml` 启用了
   `browser` / `unified-computer-use` / `visualize` / `codex-app-tools`
   四个 bundled 插件，其工具（`multi_agent_v1` 2,385 tok、`mcp__cua_repl`
   992、`mcp__node_repl` 525、goals 系列 ~1,175、MCP 资源系列 ~486）占了
   codex 工具预算的绝大部分。未擅自关闭（可能影响 codex++ 既有能力），
   需要时把对应 `[plugins."..."]` 的 `enabled` 改为 `false` 即可。

**合计效果（claude code 端到端）**：上行请求 21.1k → 8.9k tok，**降幅 ~57%**。

### 9.7 0.6.2 部署记录（2026-09-30）

四桶 + ECS LB 已全部滚动升级到 **0.6.2**，逐点验证通过。

| 节点 | 目录 | 重启方式 | 结果 |
| --- | --- | --- | --- |
| win10-local | `D:\wb_ps\proxy_opencode\repo` | 自愈循环（kill python 进程即自动拉起） | 0.6.2 ✓ |
| owin10 | `C:\Users\owin10\proxy_opencode` | `schtasks /run /tn ProxyOpencodeBoot` | 0.6.2 ✓ |
| ubuntu26 | `~/proxy_opencode` | `sudo systemctl restart proxy_opencode` | 0.6.2 ✓ |
| phone115 | `~/repo` | 自愈循环（kill python 进程） | 0.6.2 ✓ |
| ECS LB | `/opt/proxy_opencode/balancer/lb.py` | `systemctl restart zen-lb` | ✓ |

**部署要点（下次照做）**：

1. owin10 / ubuntu26 / phone115 的部署目录**不是 git 仓库**，只能同步文件：
   本地 `tar czf pkg.tgz --exclude=__pycache__ proxy_opencode` → scp → 远端
   `mv proxy_opencode proxy_opencode.bak_<日期> && tar xzf pkg.tgz`。
2. **必须同步修正 venv 里 dist-info 的 METADATA `Version:`**，否则 healthz
   仍报旧版本号（`__version__` 取自 `importlib.metadata`，不是源码）：
   `sed -i "s/^Version: .*/Version: 0.6.2/" <venv>/lib/python3*/site-packages/proxy_opencode-*.dist-info/METADATA`
3. **owin10 的 `taskkill /F` 之后自愈循环不会自动拉起**（实测），需
   `schtasks /run /tn ProxyOpencodeBoot` 补一手；其余三处 kill 后自动恢复。
4. 备份产物：`proxy_opencode.bak_0620`（三桶）、`lb.py.bak_0620`（ECS）。

**部署后实测（0.6.2）**：

- 四桶 healthz 全部 `version=0.6.2`；LB 四桶 `healthy=true`、
  `quota_exhausted=false`（win10-local 的误熔断随重启清除）。
- 公网入口简单对话 **`prompt_tokens` 2,323 → 476（-79%）**。
- 三客户端回归：hermes ✅、claude code 工具链 ✅（落盘核验）、
  codex ✅ 且 `tokens used` **6,119 → 4,081（-33%）**。
