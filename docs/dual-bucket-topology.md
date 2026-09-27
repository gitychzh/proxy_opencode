# 双桶轮询拓扑与部署运维（2026-09-28 实测基线）

> 一句话：**两个出口 IP = 两份额度，LB 在前面轮询。** 本文档记录已验证的
> 事实、部署方式、以及**已知的坑**，供后续维护直接照做。

## 0. 为什么需要这个

OpenCode Zen 的免费额度**按出口 IP 计**（429 `FreeUsageLimitError`，UTC 日
重置）。所以同一台机器上跑两个网关毫无意义——出口 IP 相同，额度是同一份。
要真正翻倍，必须让请求从**两个不同出口 IP** 出去。

本仓库的 `proxy_opencode` 负责单个出口的网关；`balancer/lb.py` 负责在多个
网关之间轮询，把两份额度合成一个入口。

## 1. 已验证拓扑（2026-09-28）

```
                        ┌─────────────────────────────────────┐
hermes (本机) ──────────>│ LB  127.0.0.1:7892  (balancer)   │
                        └───┬─────────────────────────┬───────┘
                            │                         │
                 桶 A：出口 117.95.209.16      出口 218.93.250.242
                 本机 127.0.0.1:8787          owin10 <tailnet>:8787

hermes (owin10) ─────────>│ LB  127.0.0.1:7892  (balancer)   │
                        └───┬─────────────────────────┬───────┘
                            │                         │
                 桶 A：owin10 本机（出口 218.93.250.242）
                 桶 B：<本机 tailnet>:8787（出口 117.95.209.16）
```

**关键性质**：两台机器互为对方的第二个桶，所以**两边的 hermes 都是双桶**；
任一机器的 LB 挂掉，另一台仍能通过 peer 桶继续服务。

### 部署实例

| 角色 | 主机 | tailnet | 网关端口 | 出口 IP |
|---|---|---|---|---|
| 本机（主） | `desktop-sgedrr5` | 100.121.137.118 | 8787 | 117.95.209.16 |
| 副机 | `opc-win10` | 100.109.109.108 | 8787 | 218.93.250.242 |

每台机器上跑的进程：

- `proxy_opencode`（本仓库）监听 `0.0.0.0:<8787>`，Windows 计划任务名
  `ProxyOpencode`，登出触发 + 失败重启 99 次/1 分钟。
- `balancer/lb.py` 监听 `0.0.0.0:7892`，计划任务名 `ZenLb`，配置见
  `balancer/run.cmd.example`。
- 防火墙：两个端口都需入方向放行（tailnet 访问）。

## 2. 实测数据（不要凭直觉改，先看这里）

### 双桶**不**提升单请求速度

| 指标 | 双桶 (7892) | 单桶 (8787) | 结论 |
|---|---|---|---|
| 单请求 chars/s | 288 | 300 | **-4%，无提升** |
| 4 并发聚合 chars/s | 1026 | 1043 | -2% |
| 输出内容 | 400 数字全对 / 1491 字符 | 完全一致 | 质量无差异 |

**原因**：免费层限流在**单个请求**上，一个长流式请求从头到尾只用一个桶。
开两个桶不会让任一请求跑更快。

### 双桶提升的是并发吞吐和抗排队

| 指标（本机测） | 双桶 | 单桶 | 提升 |
|---|---|---|---|
| 20 个短请求墙钟 | 94.14s | 117.26s | **-20%** |
| 平均每请求 | 4.71s | 5.86s | -20% |
| 成功率 | 20/20 | 20/20 | 持平 |

owin10 侧同样结论（20/20 vs 20/20）。**LB 是 least-connection 轮询，
分流均匀（实测 22/22、31/30、local 57 / remote 62 全程 47.9% ~ 52.1%）。**

### 故障转移（已实测，单桶宕机期间用户无感）

1. 强杀一桶进程 → LB 健康探测标记 `healthy=False`（约 20s 内）。
2. 期间所有客户端请求**全部成功**，流量自动压到另一桶。
3. 恢复进程后约 25s 内自动回池。

## 3. 验收方法（改完必须照做）

**工具调用**——判据是模型**自己写文件再读回**，不是看它打印数字：

```bash
# 探针文件
printf '3 7 11 42' > $SCRATCH/probe.txt && rm -f $SCRATCH/proof.txt
hermes --provider <provider> --model opencode/big-pickle -z \
  "用工具读 probe.txt，把数字求和写进新文件 proof.txt，再读回来报告确切内容。
   找不到文件就直说，不要编造。"
# 判据：proof.txt 存在且内容 == 63
```

**思考模式**——抓 `reasoning_content` 非空，且答案正确。

**并发**——N 路同时打 `/v1/chat/completions`，全部 200。

**分流**——LB 日志 `grep -oE '\-> (local|remote)' | sort | uniq -c` 应大致 50/50。

## 4. 已知的坑（都是实测踩出来的）

1. **工具命名冲突会静默破坏免费层校验**。客户端若提供名为
   `bash/read/edit/glob/grep/write/list/task/todowrite/webfetch/websearch/skill`
   的工具，`zen_direct.py` 的 `_merge_tools` 会让**客户端工具覆盖内置工具**，
   内置列表被挤掉 → 免费层 403，且**无任何本地报错**（只是模型不调工具）。
   → 用 `fs_read`/`fs_exec` 之类不冲突的名字。详见 `upstreams/zen_direct.py`。
2. **`stream: false` 在免费层必 403**。网关恒以 `stream: true` 调 Zen，
   非流式客户端由网关聚合 SSE。
3. **客户端文件路径要用真实绝对路径**。放到系统 temp 而模型工作目录不同 →
   模型找不到；此时模型应拒绝编造（观察到它确实这么做），但不要因此以为
   网关坏了。
4. **`Start-Process` 起的子进程会随 SSH 断开被杀**。Windows 上要用
   `Start-ScheduledTask`（计划任务拥有进程），别用 `Start-Process`。
5. **不要用 `setx` 传 `GATEWAY_API_KEYS`**——它会污染机器上所有进程的环境。
   写进启动脚本 / systemd 的 `Environment=`。
6. **日志路径不能硬编码盘符**。早期 `lb.py` 默认 `D:\...`，在没 D 盘的机器上
   `os.makedirs('D:\\')` 直接崩。现在默认用脚本同级 `logs/`。
7. **PowerShell 5.1 没有 `ForEach-Object -Parallel`**（owin10 就是 5.1）。
   并发压测用多个 python 进程。
8. **注入密钥到远端脚本时别用 `.replace("$key", ...)`**——会连 PowerShell 变量
   名一起替换，导致变量变成字面量、测试根本没发请求却"看起来跑了"。
   用 `%%PLACEHOLDER%%` 这种不可能出现在 PS 里的占位符。
9. **CF 边缘会切断 SSE**（历史坑，见 skill `llm-relay-station`）。若把链路
   暴露到公网 Cloudflare，需要 SSE 心跳保活（`: hb\n\n` 注释行，每 3-5s）。
10. **本机夜间关机**。本机 LB 和网关会一起停，owin10 侧降级为"own 单桶 +
    peer 不可用"（hermes 照常可用，已实测）。要 24h 双桶需本机常开，或引入
    第三个出口。

## 5. 客户端接入（hermes）

本机（provider `zenlb`）与 owin10（provider `renlb`）都已接入，配置形态相同：

```yaml
providers:
  zenlb:                          # 名字自定
    base_url: http://127.0.0.1:7892/v1
    api_mode: chat_completions
    discover_models: false
    key_env: HERMES_CUSTOM_ZENLB_API_KEY   # 值写在 ~/.hermes/.env
    models:
      opencode/big-pickle:
        context_length: 200000
        max_tokens: 8192
```

`config.yaml` 受保护，改它用 `hermes config set ...`（不要直接 patch）。
key 写在 `~/.hermes/.env` 的 `HERMES_CUSTOM_ZENLB_API_KEY`。

切换默认：`hermes config set model.provider zenlb`（回退 `proxyo`）。

## 6. 待办 / 下一步（正式网关与正式站点）

当前这套是**验证过的原型**，可直接日常用；但要变成"正式产品"还差这些，
按优先级排：

### P0 · 让它 24×7 可用
- [ ] 本机常开，或把 LB / 网关迁到常开机器，避免夜间单机（见坑 10）。
- [ ] 两个网关的 key 从明文 `run.cmd` / `.env` 换成系统级机密注入
      （Windows DPAPI / systemd `LoadCredential`）。
- [ ] 上游健康状态告警（LB `/healthz` 已暴露每桶 `healthy`/延迟/失败数，
      接一个定时探测 + 通知）。

### P1 · 容量与限流认知
- [ ] 找并发拐点：免费层是**累积日配额**（非瞬时限流），压到限流要烧当天
      额度，需用户拍板是否值得。当前 4 路远未打满。
- [ ] 若 Zen 将来把 `sessionId` 绑到出口 IP，跨桶轮询会触发 403；届时在
      LB 加 IP/会话哈希粘滞（当前未观察到该问题）。

### P2 · 正式对外网站（llm.223722.xyz）
- [ ] 入口层重构决策：**不要让常开的吉隆坡入口依赖夜间关机的本机 7892**。
      正确形状是入口常开、桶在常开侧，或 7892 在入口机重建。
- [ ] 现状：KL 入口机（`47.250.130.52`，SWAS，tailnet `100.90.84.65`）上的
      llmgw 上游指向 HM2 的 docker 端口（46666/40777/47777/45001）。`v4f45001`
      当前走的是 HM2 的 `dir45001`（借官方二进制 TLS 终结的 hack），**不是**本
      仓库这套双桶 proxy_opencode。要纳入需评估替换。
- [ ] Cloudflare：`llm.223722.xyz` → CNAME `8e86d61b-...cfargotunnel.com`（橙云）。
      现有 API token 只有 zone DNS 权限，**缺 `Account:Cloudflare Tunnel:Read`**，
      列 tunnel / 读 ingress 都 401。需补一个带 Tunnel 权限的 token。
- [ ] 公网 SSE 需心跳保活（坑 9）。

### P3 · 工程化收尾
- [ ] `balancer` 的 pytest 纳入 CI（当前 `pytest.ini` 在 `balancer/` 下，
      与根 `pyproject.toml` 的测试发现并存，需在 `ci.yml` 里显式跑）。
- [ ] 版本与 CHANGELOG 随每次拓扑/协议变更递增（当前 0.4.0）。

## 7. 相关文件

| 路径 | 说明 |
|---|---|
| `balancer/lb.py` | 轮询 LB（纯 ASGI，无框架依赖） |
| `balancer/tests/test_balancer.py` | 10 项单测（含 failover、key 改写、错误语义） |
| `balancer/run.cmd.example` | 配置模板（复制为 run.cmd 后填 key） |
| `upstreams/zen_direct.py` | Zen 协议重构（标记注入 + 内置工具合并） |
| `upstreams/zen_prompt_default.txt` | 免费层标记资产，须与 opencode 版本同步 |
| `docs/engineering-constraints.md` | 工程红线 |
| `AGENTS.md` | 维护约定与免费层三要素（**改本项目前必读**） |
