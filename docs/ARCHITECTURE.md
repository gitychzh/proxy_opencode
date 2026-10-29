# 架构（ARCHITECTURE）

> 0.7.x 新栈的权威描述：网关整体运行在 ECS 的一个 Docker 容器里，出口设备只跑
> `egress-agent`。**单栈**——旧栈（ECS `zen-lb` + 各设备完整网关，入口 `9443`）
> 已于 2026-10-17 从 ECS 清退，见 §10。
>
> 免费层多层门（形状 / 传输栈 / session / body / 模型）与 429 归因红线是
> **协议事实**，唯一权威在 [`AGENTS.md`](../AGENTS.md) 硬约束 1，本文不复述。

## 1. 拓扑

```
hermes / codex / 任意 OpenAI 兼容客户端
   │ http://115.29.231.25:10002/v1    (明文 + API key；唯一入口)
   │ (无域名入口：2026-10-29 起 223722.xyz 的 LLM 记录与隧道全部删除，
   │  域名为空，需 ICP 备案后才可能重建)
   ▼
ECS ┌───────────────────────────────────────────────────────────────┐
    │ docker zen-gw-edge  :10002（公网唯一入口；只做透传 + 路由选择） │
    │   ├ 探针 GET http://zen-gw:10002/healthz 每 2s                  │
    │   ├ primary  → http://zen-gw:10002            （正常：全透传）  │
    │   └ fallback → http://172.17.0.1:10003        （proxy_ms 兜底） │
    │                        │                          ▲            │
    │ docker zen-gw（网关本体；compose 网络内部，不发布端口）          │
    │  ├ routes/     chat / models / responses / anthropic / admin    │
    │  ├ upstreams/  zen_direct、bun 传输、failover（进程内备用链路） │
    │  ├ egress/     pool、counters、rotate、catalog、eventlog        │
    │  └ edge/       公网入口那层（与 zen-gw 同镜像，command 不同）    │
    └───────┬───────────────┬───────────────┬────────────┬──────────┘
       Tailscale 100.x（+ basic auth）          loopback │
      ┌─────┴─────┐   ┌─────┴──────┐   ┌────┴─────┐  ┌─┴──────────────┐
      ▼           ▼   ▼            ▼   ▼          ▼  ▼ esp32 适配器     │
 owin10:10001  owin10:10002    cc9:10001        :10005（:9999 反向隧道）
 移动有线       电信 WiFi       联通               ESP32/MiFi 移动
      └─────────────┴───────────────┴──────────────┴──▶ opencode.ai/zen/v1
```

五跳：客户端 → **edge（公网 10002）** → 网关容器 → 设备 agent 的 CONNECT 隧道
→ Zen。0.8.0 把入口和网关拆成两个容器：上游的形状门（内置工具集 / prompt 模板 /
日期）让网关必须频繁改动，而入口一旦跟着重建就等于把用户踢下线。edge 只做两件事
——转发、以及在**首字节到达客户端之前**改投 `proxy_ms`，所以重建网关对用户是
"慢一点"而不是"连接错误"。明文入口（2026-10-28 起）让客户端只需 API key，不装证书。
出口 IP 是各设备自己的公网 IP，也就是免费层计量的维度。上游 TLS/HTTP2 那一跳
由 `zen_bun/forward.ts` 一次性子进程承担（传输栈门），每次上游试探以当前租得桶
的 agent 作 HTTPS 代理出站（Basic 凭据即时拼装，不落日志/spec）；池选择、
cordon、换 IP、failover 语义与传输无关。

## 2. 端口注册表（统一口径）

| 端口 | 归属 | 说明 |
|---|---|---|
| **10001** | 设备侧 egress-agent | CONNECT + `/healthz` + `/rotate` 同端口（owin10-mobile / cc9） |
| **10002（设备）** | egress-agent 第二实例 | 同设备第二上行（owin10-telecom） |
| **10002（ECS）** | `zen-gw-edge` 容器发布 | 客户端入口，明文 HTTP + API key（0.8.0 起由 edge 独占；此前是网关本身直接发布 → 0.7.x 早期是 caddy TLS 反代 `127.0.0.1:10010`） |
| **10002（compose 内）** | `zen-gw` 容器 expose | 网关本体，只在 compose 网络内被 edge 调用，不发布到宿主 |
| **10010** | 容器宿主映射 | `since-retired`（2026-10-28）：旧 loopback 运维入口，随 caddy 链路一并下线 |
| 10003 | proxy_ms（ECS 宿主） | 备用链路网关，见 §7 |
| 20001 / 20002 | proxy_nv（ECS 宿主，caddy `:20002`） | 与本栈无关，不动 |
| 9443 / 7892 / 8791 / 8793 | 旧栈 | `since-retired`：9443/7892/8793 于 2026-10-17 清退；**8791/8793 于 2026-10-29 彻底下线**（`ProxyOpencodeBoot` 计划任务已注销，`C:/Users/owin10/proxy_opencode/` 部署树、日志、payload_dump 已删，仓库 `start_gateway.bat` 已移除）。最后成功业务调用 2026-10-03，此后仅旧 LB 健康检查空转 |
| 443（域名入口） | 223722.xyz | `since-retired`：**2026-10-29 域名入口整体删除**——`llm` / `llm2` / `gw2` / `zen-gw` / `zen` 五条 DNS 记录删除，`zen-gw` 与 `llm-gateway` 隧道 delete，ECS 上 `cloudflared` 二进制 + unit + 配置 + 凭据全清。重建需先 ICP 备案 |
| 40016 / 40066 | GLM 网关 | 与本栈无关，不动 |

安全组只放行 10002。设备侧 10001/10002 只听 Tailscale，另有 agent basic auth。

## 3. 组件地图

| 路径 | 内容 |
|---|---|
| `proxy_opencode/` | 网关：`routes/`（薄路由）、`formats/`（协议转换）、`upstreams/`（zen_direct / bun 传输 / failover）、`egress/`（pool / counters / rotate / catalog / eventlog / updater）、`edge/`（公网 10002 透传层：policy / app / probe） |
| `egress-agent/` | 设备侧 Python 包：CONNECT 代理 + healthz + ONT 换 IP；`deploy/` 含 systemd 与 Windows schtasks 两条已验证路径 |
| `egress-agent-android/` | 原生 Kotlin 版占位（wire 契约已冻结，P6） |
| `egress-agent-esp32/` | ESP32 桶（移动 MiFi 上行）：PlatformIO 固件 + 网关侧反向隧道适配器 + MiFi 换 IP 工具；线协议契约 v4（HELLO 共享密钥鉴权） |
| `deploy/ecs/` | Dockerfile / compose / env.example / Caddyfile 片段 |

## 4. 分期与状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 方案冻结、骨架、CI 双端门禁、端口注册表 | 完成 |
| P1 | egress-agent 数据面（CONNECT / healthz / rotate）+ 设备实测 | 完成 |
| P2 | egress pool + 容器化，ECS:10002 起容器 | 完成 |
| P3 | catalog 反代白名单 + 隐藏 `/v1/models_cus`，严格别名 | 完成 |
| P4 | updater：每日查 GitHub release 分级采用 | 未开始 |
| P5 | 换 IP：计数窗口 + 阈值/硬 429 触发 + ONT 重启 + IP 校验 | 完成（实弹） |
| P6 | Android Kotlin agent、文档浓缩定稿 | 未开始（Windows agent 已先行） |

## 5. 公开模型目录

- 对外唯一别名 `deepseek-ai/deepseek-v4.1-flash`，`/v1/models` 返回严格 OpenAI
  形状（`id/object/created/owned_by`）。**只有 `PUBLIC_MODELS` 里的 id 可请求**，
  上游裸 ID 一律 404（`registry.resolve` 不接受上游拼写）。
- 别名 → 上游免费裸 ID 的改写由 `PUBLIC_MODELS` 第三列决定，`ZEN_MODEL_MAP`
  （`from=to` csv，精确命中优先于 provider 前缀剥离）可覆盖。生产当前上游为
  `big-pickle`；免费层只服务免费 ID（`-free` 后缀或 `big-pickle`），付费裸 ID
  回 401（模型门，见 AGENTS）。
- `OPENCODE_ZEN_MODELS` 是唯一反代白名单。`egress/catalog.py` 每 10 分钟拉
  `GET https://opencode.ai/zen/v1/models`，按 `allowAnonymous`/`cost`/`pricing`
  （缺标记时按 `-free` 后缀）判免费，再与白名单求交集——不枚举全部免费模型，
  不裸 ID 透传。
- `/v1/models_cus?key=`（隐藏端点）是唯一展示被反代裸 ID 的地方：默认纯文本
  一行一个 ID，`?format=json` 才给 OpenAI 形状。标准 `/v1/models` 只列别名，
  **不要**再造 models/model 之类普通变体端点（会被扫描 agent 捕获）。

## 6. 出口池语义

- **选桶**：least-inflight + 轮转 tie-break，全空闲时不会钉死字母序最前的桶。
- **健康**：`healthy = agent ready 且探针 HTTP 200`。设备侧 `ready` 还要求探针
  新鲜（`PROBE_READY_GRACE_S=45s`）且最近一次探测未失败；网关侧再把
  `egress_probe_ok is False` 直接判 unhealthy（两层防御，防僵尸桶）。**探针自身
  的传输异常（连不上 agent）同样是健康判决**：落 `healthy=false` + `probe_failed`
  事件，按状态跃迁记一行 WARNING，不打 traceback（2026-10-25 前此处异常会逃逸，
  死 agent 继续接客直到 3 次请求级失败）。IP 前缀匹配只作审计字段，不参与健康
  判定，也不作为 cordon 解除的前置。
  **软失败有容忍度**（0.8.2 起）：200 但 `ready=false` / `egress_probe_ok=false`
  属「agent 活着、出口腿短暂不通」，连续 `ZEN_EGRESS_PROBE_FAIL_TOLERANCE`
  次（默认 1 = 历史行为，生产设 2）才摘桶；未达阈值只记 INFO、不发事件。
  传输异常与「非 200 且未就绪」仍是硬判决，第一次即摘桶——两者必须分开：
  前者是 LTE 丢包（2026-10-29 实测 5h/13 次，全在下一探针自愈，桶 `failures=0`），
  后者是 agent 真的没了（2026-10-25 僵尸桶）。
- **cordon 与降权（两档，0.8.5 起）**：429 烧穿（`free_quota_exhausted`，绑到窗口末）
  与 403 门槛（`free_tier_rejected`）都是**对出口 IP 的判决**——探针观测到 IP 变化
  且就绪时立即解除（`cordon_lifted`，IP 不在 `expect_prefix` 内时另记
  `prefix_mismatch` 审计事件）。**传输失败（任何次数）永不 cordon**（0.8.5 起，
  演练推翻了「3 次才 escalate」——那是同一缺陷的放大版）。**软 429**
  （`Retry-After`≤3600s）是突发节流而非额度判决，只 cordon 该时长
  （`free_quota_soft`）。重复判决只静默延长租约、不重复记事件，更短的判决不得覆盖
  更长的存活 cordon。
  与 cordon 并列的是**降权（cooldown）**：传输失败/5xx **不移出轮换**，只把桶
  排到选择顺序末尾（`ZEN_EGRESS_COOLDOWN_S=20` × 连续次数，上限
  `ZEN_EGRESS_COOLDOWN_MAX_S=300`），该请求由备用链答完，下一请求照常轮询（含它
  自己），成功一次立即清零。0.8.5 起**任何次数的传输失败都只降权**，递增窗口
  （封顶 300s＝旧 cordon 长度）独自承担「这条腿已死」的信号；cordon 只留给
  IP 域判决。选择顺序为
  正常健康桶 → 降权桶 → last-resort 桶 → cordon 桶。
- **首字节（TTFB）超时**：`ZEN_TTFB_S`（默认 60s；实测 TTFB p95=21s、p99=305s）只约束**响应头**，到达即解除；
  之后的静默由 `STREAM_STALL_S` 管。此前整个交换只有 `zen_timeout_s=300` 一个
  上界，隧道黑洞会让单个请求先烧掉 300s 再换桶（实测 305–315s）。
  **降权桶用更短的 `ZEN_TTFB_DEMOTED_S`（默认 15s）**：60s 是给首次尝试的耐心，
  一个刚失败过的腿不该再拿走用户第二个满分钟。该值只缩短不延长，0 关闭分级；
  bun 逐次覆盖 `ZEN_BUN_TTFB_MS`，httpx 路径作用于响应头阶段（正文仍归
  `STREAM_STALL_S`）。
- **换 IP**：每桶日窗 `[08:00, 08:00+1d)` Asia/Shanghai（=UTC0）计数。**主触发是
  token 额度** `ZEN_EGRESS_TOKEN_ROTATION`（每桶每窗口的估算 prompt tokens 之和）：
  上游按 token 计量而非请求条数（2026-10-28 实测 166 条请求即烧穿一个 IP 的日额度，
  约 10.3M prompt tokens），条数阈值 `ZEN_EGRESS_ROTATION` 只作为粗粒度副闸——
  它在额度烧完后才可能到达，单独使用等于"事后补救"。两者任一命中即触发；硬 429 /
  403 门槛绕过阈值立即触发，共用"每窗口每桶一次"去重与失败冷却
  （`ROTATION_RETRY_COOLDOWN_S=900s`），硬 429 路径另有
  `ZEN_EGRESS_ROTATE_MIN_INTERVAL_S=1800s` 下限防风暴。
  token 计量的口径：`ZenDirectAdapter` 把上下文闸已算出的估算 prompt 数
  （`Applied.estimated_input`）**戳在 Lease 上**，仅在 `ok` 释放时折叠进计数
  （429/5xx 不消耗额度，不得计入）。
  编排：标记 rotating（选桶跳过、在途自然收尾）→ `POST {agent}/rotate` →
  轮询 `/healthz` 等 IP **变化**（落在 `expect_prefix` 内只作审计，不符时记
  `prefix_mismatch`，不再判失败）→ 清 cordon、计数归零。**`POST` 传输失败不等于
  最终失败**：命令可能已送达或 ONT 自行重拨，照常轮询到 deadline，观测到合法新
  IP 即成功；明确 HTTP 拒绝、或 IP 在 deadline 内未变，才是失败。
  计数快照落 `ZEN_EGRESS_COUNTS_PATH`（`/data` 卷），跨重启存活。
- **流看门狗**：`STREAM_STALL_S`（上游静默）与 `CLIENT_STALL_S`（客户端写被
  背压阻塞）默认 60s；超时合成 `finish_reason:"length"` + `[DONE]` 优雅收尾，
  并按结局释放租约（`stalled`/`interrupted` 记失败，3 连败 cordon；`cut` 与
  客户端断连仍按 ok）。
- **429 兜底**：桶间 failover 上限 min(桶数, 3)；兜底耗尽时匿名 403
  `FreeTierError` 改回 **503 + `egress_unavailable`**（可重试语义），不再透传
  成客户端的 auth 错。
- **管理接口**：`GET /admin/egresses`（含 `counters` 块）、
  `POST /admin/egresses/{name}/rotate`（管理员 key），仅 Tailscale/loopback。

## 7. 备用链路：zen-direct 故障 → proxy_ms

主链路发生**出口级**故障时，请求改由同机 `proxy_ms`（ModelScope 聚合网关，
宿主 `172.17.0.1:10003`）服务，窗口结束自动回到主链路。

- **形态**：`upstreams/failover.py` 的 `FailoverAdapter` 包在 `build_adapter`
  外层（覆盖 chat/completions、completions、responses、`/v1/models`）；
  `ZEN_FALLBACK_BASE_URL` 为空则完全不包装，行为零变化。
- **触发（仅出口级）**：`ConnectionError`，或上游错误状态码 ∈
  {429, 500/502/503/504}（含 zen 自己的 `egress_unavailable` 闸门）。
  **请求级 4xx 不切换**——换供应商只会把错误变成另一种错误。
- **熔断**：触发后进入 `ZEN_FALLBACK_HOLD_S`（默认 120s），窗口内不再打主链路；
  窗口后第一个请求做半开探测。**闭合需要连续 `ZEN_FALLBACK_RECOVER_AFTER`（默认 2）
  次主链路成功**，任一次失败即清零重入——单次成功就闭合会让主链路半好时按请求速率
  来回切换（2026-10-25：单日 57 engaged / 11 recovered，且每次都把客户端正在用的
  模型换成备用的 ID）。同一轮故障内的再次跳闸只记 INFO `FALLBACK RE-ENGAGED`
  （事件带 `re_trip=true`），真正的恢复才记 WARNING `FALLBACK RECOVERED`。
- **事故级留痕（2026-10-27）**：首次跳闸记 ERROR `PRODUCTION INCIDENT -
  FALLBACK ENGAGED`（INFO 过滤的采集也必留存）；恢复记 WARNING
  `PRODUCTION INCIDENT CLOSED - FALLBACK RECOVERED`；备用也不可达/回错时
  ERROR `PRODUCTION INCIDENT - FALLBACK FAILED`。JSONL 事件带 `severity`
  字段：`incident`（首次/备用失败）/ `continuation`（re-trip）/
  `recovered`（闭合）。
- **模型改写**：包装层拿到的是路由层已解析的 ID（别名到此处已是 `big-pickle`），
  所以 `ZEN_FALLBACK_MODEL_MAP` 的键必须是解析后的上游 ID，
  `ZEN_FALLBACK_DEFAULT_MODEL` 兜住未命中项。
- **备用返回错误时释放窗口**：proxy_ms 欠费会回 `429 insufficient balance`。
  此时**原样回给客户端**（诚实暴露备用故障）、记 ERROR `FALLBACK FAILED` +
  JSONL `fallback_failed{status}`，并**清零熔断窗口**让下个请求重新探测主链路
  （主链路 429 可能只是 body/传输/session 门误伤）。备用**不可达**
  （`ConnectionError`）仍保持熔断。
- **观测**：WARNING/ERROR 三态日志、JSONL 事件
  `fallback_engaged/recovered/failed`、`/healthz` 的 `fallback` 块
  （`engaged` / `secondary` / `hold_remaining_s`）。

## 8. ONT 换 IP 拓扑

2026-10-25 现役两桶，均实弹验证。opc2 三桶随设备永久下线，仅作协议参考。
凭据只存设备 JSON（Linux `/etc/egress-agent/*.json`、Windows
`C:\egress-agent\*.json`，600），仓库只记型号与拓扑。

| 桶 | 出口 | 管理地址 | 型号 | 驱动 `kind` |
|---|---|---|---|---|
| `owin10-mobile` | 223.68.x / 36.149.x 移动 | 192.168.1.1（`interface=192.168.1.222`） | 华为 V173-50 | `huawei`（四步 HTTP） |
| `owin10-telecom` | 106.111.x / 121.234.x 电信 | 192.168.1.1（`interface=192.168.2.235`） | 华为 HG8020C | `huawei_ct`（token + 无状态 Cookie + CtcApp ResetBoard） |
| ~~opc2-eth / opc2-wifi / cc9~~（历史） | 移动 / 电信 / 联通 | — | V173-50 / HG8020C / 烽火 HG6142A-C | `huawei` / `huawei_ct` / `fenghuo` |

驱动共性坑：①电信 CtcApp 页树间歇 404（47 字节 "The requested URL..."），需整
周期重试（`CT_ARM_ATTEMPTS=2`）；②烽火 login GET 必须带 `tkn=完整 UA`，否则只
回 `random_string`；③光猫对短时连续重启有防抖（受理但不重启），由 900s 冷却
兜底；④**Windows 侧 `onts[].interface` 必须写字面 IPv4**——`SIOCGIFADDR` ioctl
只存在于 Linux，写接口名会被静默忽略、源地址不钉死，两台 ONT 都在
`192.168.1.1` 时会打错光猫（`source_address()` 同时接受接口名与字面 IPv4）。

## 9. 关键决策（ADR 摘要）

| 决策 | 结论 | 理由 |
|---|---|---|
| agent 语言 | Linux/Windows=Python；Android=Kotlin（占位） | 稳定优先；现役设备都能跑 Python（含 Android chroot） |
| 第三方依赖 | 成熟库优先（aiohttp / requests），仅 CONNECT 双工管道自写 ~100 行 | proxy.py 过重且源地址绑定能力不明确 |
| TLS | **IP 入口无 TLS（明文 HTTP）**，2026-10-28 起取消 caddy 自签 CA 中转；域名入口 + LE 方案保留未启用 | 客户端装证书代价大于收益（用户否决）；域名链路实测 23–68s 不可用 |
| 计数器 | 每出口日窗 [08:00, 08:00+1d) Asia/Shanghai，≥500 触发换 IP，换完清零 | 与上游 UTC0 重置对齐；上游真实额度 ≪500，阈值仅作兜底 |
| 403 门槛 | 释放为失败 + 立即 cordon + 绕过阈值换 IP | 曾被当请求级 ok 释放 → 用户持续报错；根因是形状门，防御保留 |
| 桶间 failover | 429/5xx/传输失败/匿名 403 → 换桶；SSE 开播后不换；兜底 503 | 单桶故障时别桶仍可用；透传 403 会被客户端判不可重试 |
| updater 分级 | T1 自动（prompt 资产/UA）、T2 冒烟后自动（算法常量）、T3 只报告 | 协议常量变化必须过冒烟闸门 |
| 出口绑定 | Linux `bind((source,0))` 或 uid 策略路由；Windows 字面源 IP | 与旧网关同机制，真机已验证 |

## 10. 历史要点与现状偏差（仅防误读）

- opc2 双上行（wifi/eth）靠 uid 策略路由区分出口，ONT 重启会打掉规则与表默认
  路由，需 `policy-routes-keep.timer` 幂等重放；现役 Windows 设备改用
  `bind_source` 钉源，不再需要。
- cc9 曾在 Android chroot 内跑 Python agent（Magisk `service.d` 看门狗），出口
  IPv6-only，靠 asyncio 按 getaddrinfo 快速失败回落。
- 平台差异：Linux 用 systemd 模板单元；Windows 用 Task Scheduler（SYSTEM，
  ONSTART）+ `bind_source`；agent 配置若由 CRLF 文本生成，Basic 凭据会带 `\r`
  导致 CONNECT 407。设备系统 Python <3.12 时装 `/opt/egress-agent/venv`（绕开
  `ProtectHome`）。

## 11. 验收判据

- 每阶段：两侧 `pytest` + `ruff` + `mypy` 全绿（清单见 AGENTS 硬约束 3）。
- 涉及设备或 ONT 的改动必须有真机验证；入口/模型变更必须跑一次本机 hermes
  全链路 e2e。
