# Changelog

语义化版本；格式遵循 [Keep a Changelog](https://keepachangelog.com/)。
协议事实的权威表述在 [`AGENTS.md`](AGENTS.md) 硬约束 1，本文只记变更。

## [0.7.0] - 2026-10-27

> 本节覆盖 v0.5.0 以来 0.6.x→0.7.x 的全部累计变更（107 commits，2026-10-13 →
> 2026-10-27），含 0.7.0a1–a5 五个 alpha。0.7.0 起为生产稳定版。

### Added

- **feat(esp32): 控制通道 HELLO 鉴权（协议契约 bump v4）** — 适配器 :9999 必须公网
  可达（ESP32 从 MiFi NAT 后外连），v3 实测有公网扫描者连入（azenv/SSH 探测），
  且 HELLO 无鉴权＝任何主机可冒充 ESP32 抢占桶槽并收走 CONNECT 流量。v4 在
  `HELLO <name> <lan_ip>` 追加第 4 段共享密钥（固件 `CTRL_SECRET` 常量 / 适配器
  `ESP32_CTRL_SECRET` 环境变量），不匹配回 `BYE` 立即断开；适配器侧密钥为空时
  退回 v3 兼容免鉴权。同批把适配器收编为 ECS systemd 单元
  `esp32-bucket-adapter.service`（此前 nohup 裸进程，无自愈，本次排查中即因
  ssh 会话退出而双实例/全灭过）。实测：假 HELLO 被拒并留痕、真 ESP32 正常注册、
  CONNECT 数据面 2/2 成功。
- **feat(egress): 新增 `egress-agent-esp32/` 模块（ESP32 桶，移动 MiFi 上行）** —
  原 `esp32_p1` 独立仓库合并为子模块：PlatformIO 固件（反向隧道、双网 fallback、
  并发信号量）+ 网关侧适配器（`esp32_bucket_adapter.py`，标准桶接口
  `CONNECT`+`/healthz` :10005）+ MiFi 换 IP 工具（`tools/reboot_mifi.py`）。
  线协议契约冻结 v3 见模块内 `PROTOCOL.md`（改协议先改契约）。
  接入桶池：`ZEN_EGRESS_PROXIES` 追加 `esp32=http://127.0.0.1:10005`。
  实测：CONNECT/HTTP 全链路、8 并发（4 立即+3 排队+1 超时）、断链自愈、
  MiFi 重启后全链路恢复。
- **feat(egress): 按请求数的周期性出口轮换（每桶 500 条成功请求换 IP）** — 此前
  轮换只由硬 429 / 403 触发（即「烧穿之后才换」），运维侧要求在配额耗尽**之前**
  主动换 IP，避免单桶当日成功请求数逼近免费层上限触发 429。新增
  `ZEN_EGRESS_ROTATION`（如 `owin10-mobile=500,owin10-telecom=500,cc9=500`）：
  `counters.record()` 后达到阈值即 `rotation_requested`（trigger=`threshold`，
  ONT 重启换 IP），计数器随成功轮换归零重新武装；与既有机制协同——硬 429 仍
  绕过阈值立即轮换、`HARD_ROTATE_MIN_INTERVAL_S` 下限防抖、失败重试冷却。
  测试 `test_threshold_triggers_rotation_and_resets_counter` /
  `test_threshold_fires_exactly_once_and_resets_on_rotation` /
  `test_settings_parse_rotation_thresholds`。

- **feat(failover): 兜底启用升格为生产事故级记录** — 运维侧要求备用链路接管时
  「醒目提醒 + 日志留痕」。首次跳闸从 WARNING 升为 **ERROR**
  `PRODUCTION INCIDENT - FALLBACK ENGAGED`（INFO 过滤的采集也必留存，
  `PRODUCTION INCIDENT` 前缀可直接 grep 告警）；同一轮故障内的再次跳闸仍是 INFO
  `FALLBACK RE-ENGAGED`（continuation，不重复报事故）；备用链也不可达时 ERROR
  `PRODUCTION INCIDENT - FALLBACK FAILED`；恢复闭合记 WARNING
  `PRODUCTION INCIDENT CLOSED - FALLBACK RECOVERED`。JSONL 事件新增 `severity`
  字段（`incident` / `continuation` / `recovered`），事件账本可按严重度过滤。
  测试 `test_first_trip_is_error_level_production_incident` /
  `test_incident_severity_lands_on_jsonl_events` /
  `test_backup_failure_is_marked_as_incident` /
  `test_re_trip_keeps_continuation_severity`。

### Fixed

- **fix(egress): 探针成功不再清零请求级失败连击（esp32 僵尸桶当日 43/44 失败从未
  cordon）** — `_probe_one` 成功路径无条件把 `failures` 清零并刷 `egress_recovered`
  事件，本意是「agent 恢复后给干净档案」。但 `/healthz` 只反映 agent 控制通道：
  2026-10-27 esp32 桶的 ESP32 控制通道全天在线（探针 30s 一次全绿），其移动 MiFi
  数据面却已劣化（6 次 CONNECT 2 次超时，1MB 下载仅 6.7KB/s），客户端请求 44 次
  首跳 43 次 `502 Tunnel Failed`/`ECONNRESET`。每次探针成功都把请求失败连击在
  凑满 3 之前抹掉，3 连败 cordon **永远无法触发**，least-in-flight 继续把 ~25% 流量
  送进坏桶（当日 44 次 `egress_recovered` 事件刷屏佐证）。现在失败连击只能由
  **成功的请求**（`release(ok=True)`）或**真实的 agent 恢复**（探针从 unhealthy→
  healthy 的跃迁，即控制通道确实断过又回来）清零；`egress_recovered` 事件也随之
  只在跃迁时发一次，不再每 30s 刷屏。测试
  `test_probe_success_keeps_request_failure_streak` /
  `test_probe_recovery_after_outage_resets_and_emits_once`。

- **fix(failover): 备用链路错误不再顶替主链路身份（billing 429 归因错乱）** —
  2026-10-27 00:03Z 客户端收到 `provider: oc1id / layer: billing / retryable:false /
  HTTP 429: insufficient balance`：proxy_ms 的计费 429 被原样中继，客户端读成 zen
  主链路的配额墙并放弃重试，运维还会误判为「该换 IP」。现在 `_via_secondary` 把
  备用错误重塑为网关自有的 `503 fallback_backup_error`（消息只带备用地址与原状态
  码，不回显上游正文），并照旧清零 `_fallback_until` 让下一请求立刻探测主链路。
  测试 `test_backup_error_body_is_not_echoed_to_client` +
  `test_backup_error_relay_releases_hold_for_next_primary_probe`。

- **fix(responses): 生命周期事件名改回 `response.in_progress`** — 流式 `/v1/responses`
  在 `response.created` 之后发的是裸 `in_progress`，而 Responses 线上事件名是
  `response.in_progress`。`event:` 头与 `data.type` 都不匹配任何 SDK 的分派表，
  客户端**静默丢弃**该事件：网关自己的流看着完整，codex 之类客户端却看不到进度
  态。测试 `test_responses_endpoint_streams_masked_events` 现在同时断言正确名存在、
  裸名不存在。

- **fix(routes): 聚合响应体的"合成 SSE"分支补上 `relay_stream`** — `/v1/responses`
  与 `/v1/messages` 在客户端 `stream=true`、上游却返回整包 JSON 时，直接把单块
  SSE 交给 `StreamingResponse`，绕过了 `relay_stream`：因此**没有 `stream ended`
  终态记录**（延迟/结束原因丢失），也没有客户端写停顿看门狗。现在两条路由都走同
  一个包装器，成功率与流生命周期记账和真流式一致。测试
  `test_stream_from_aggregated_body_still_logs_the_stream_end`。

- **fix(egress): 租约释放幂等 + 取消时归还 in-flight 名额** — `Pool.release` 无条件
  `inflight -= 1`，而传输层存在「先 `release` 再抛出」的路径（bun 传输失败后
  `raise ConnectionError`），网关的 `except` 分支又不会再释放一次——两种写法叠加
  会让一次请求的释放被记两次，**把别的请求的 in-flight 名额也减掉**，least-in-flight
  选择因此长期偏斜。`Lease` 增加 `released` 标记，重复判决直接忽略（并记 debug）。
  同时新增 `Pool.abandon(lease)`：客户端中断 / 任务取消这类「桶没有过错」的退出
  只归还名额，不记成功、不记失败、不写事件、不 cordon；适配器在
  `except BaseException` 里调用它再重新抛出（此前的写法会永久泄漏名额，只能靠重启
  进程回收）。测试 `test_release_is_idempotent_for_the_same_lease` /
  `test_abandon_returns_the_slot_without_a_verdict`。

- **fix(egress): 探针容忍非对象 JSON** — `_probe_one` 对 200 响应只做
  `payload = resp.json()`，随后 `payload.get(...)`。agent 返回数组或裸字符串时
  会抛 `AttributeError` 逸出探针循环（与 1c2413d 修掉的「僵尸桶」是同一处代码
  路径的另一种触发形式）。现在非 dict 载荷按空对象处理，走正常的
  healthy/unhealthy 判定。测试 `test_probe_tolerates_non_object_health_payload`。

- **fix(bun): `ZEN_TTFB_S=0` 必须显式导出 `ZEN_BUN_TTFB_MS=0`** — 文档语义是「0
  关闭额外首字节计时器」，但 Python 侧只在 `ttfb_s > 0` 时才设置该环境变量；
  变量缺失让 `forward.ts` 落到默认 `60000`ms，于是"关闭"实际变成"60 秒硬截止"。
  改为无条件导出（含 `"0"`）。测试
  `test_post_without_header_deadline_exports_explicit_zero`（旧用例断言"不导出"，
  即把缺陷写成了契约）。

- **fix(failover): 每请求的 `FALLBACK ROUTING` 从 WARNING 降为 INFO** — 这是一条
  **正常路由**记录（备用链路启用期间每个请求一行），24h 实测 238 行，把真正的
  WARNING 淹在同一级别里。跳闸/恢复的状态跃迁日志保持 WARNING/INFO 原有级别。

- **fix(errors): 只有显式请求错才回 400，其余 ValueError 一律 500** — 管线把适配器
  抛出的**任何** `ValueError` 映射成 `invalid_request_error`，包括上游响应体解析
  意外（线上 2026-10-25 一例：2.2s、`usage=null`、HTTP 400）。后果有两层：把上游
  故障说成"你的请求有错"，且请求级 4xx **不参与桶间 failover**，等于同时损失可用
  性。新增 `errors.InvalidRequest(ValueError)` 作为唯一的客户端错标记
  （`_build_body` / `tools_contract` / `anthropic_proto` 三处校验改用它），
  `dispatch` 仅对 `InvalidRequest` 回 400，其余 `ValueError` 回 500 并记 ERROR
  （不带正文）。同时 `_aggregate_sse` 对非数值 `created` 容忍。测试
  `test_bare_valueerror_from_adapter_is_a_500_not_a_400` /
  `test_invalid_request_still_maps_to_400`。

- **fix(zen): 上游跳增加首字节（响应头）超时** — 生产实测 12h 内 5 次客户端请求
  在 305–315s 完成、其后流本身只需 0.5–4s：`zen_timeout_s=300` 是整个交换的唯一
  上界，黑洞隧道（agent 掉线 / ONT 重启 / CONNECT 不响应）会用掉全部预算才轮到换
  桶。`forward.ts` 新增 `ZEN_BUN_TTFB_MS`（响应头到达即清除该定时器，之后的静默
  交给既有流看门狗），Python 侧 `post()` 的 META 等待也改用该预算兜底，配置项
  `ZEN_TTFB_S`（默认 60s，0 关闭）。同时 `_log_failover` 带上传输失败原因——此前
  300s 悬挂在日志里**零痕迹**，因此长期未被发现。测试
  `test_post_fails_fast_when_no_headers` /
  `test_post_env_carries_header_deadline` /
  `test_ttfb_deadline_default_and_env`。
- **fix(egress): 探针传输失败即判不健康（消灭僵尸桶）** — `_probe_one` 的
  `ConnectTimeout` 会逃逸到探针循环，被 `logger.exception` 打成完整 traceback
  「probe failed unexpectedly」，且 `healthy`/`last_probe_ok` 完全不更新：agent
  已死的桶继续接客，直到 3 次请求级失败（线上出现「10 consecutive failures
  (transport)」连续串与同桶 305s 悬挂）。现在传输异常/未就绪一律落为
  `healthy=false`、`probe_failed` 事件、单行 WARNING（仅按状态跃迁记录），探针
  改用共享连接池。测试 `test_probe_connect_timeout_marks_bucket_unhealthy` /
  `test_probe_failure_logs_once_per_transition`。
- **fix(egress): cordon 只在状态跃迁时记账 + 软 429 缩短租约** — 每个 429 都会
  重新 cordon 到 UTC0 并重发 ERROR 与 `cordon` 事件（单 6h 48 条 ERROR、单日
  ~250 条事件），把唯一可用于归因的事件账本淹掉；且 `Retry-After` 为软限流
  （≤3600s）时同样被禁到当日窗口末，等于一个 30s 节流罚掉一整天容量。现在重复
  判决只静默延长、跃迁才记账，软 429 按 `Retry-After` 设短租约，更短的判决不得
  覆盖更长的存活 cordon。测试 `test_repeated_429_reports_one_cordon_transition` /
  `test_soft_429_cordons_for_retry_after_not_the_whole_day` /
  `test_late_hard_429_does_not_shorten_a_live_cordon`。
- **fix(egress): `expect_prefix` 降级为审计字段** — 前缀是手工维护的白名单，却被
  当作「解除 cordon」与「轮换成功」的硬前置。线上 owin10-telecom 合法出口
  `117.95.178.237`（及多个 `49.x`/`121.234.x`）不在名单内，导致 cordon 永不解除、
  单日 11/21 次轮换被判失败，与 ARCHITECTURE §6「前缀只作审计」自相矛盾。现在 IP
  真的变化即可解除 IP 域 cordon 并接受轮换结果，不符时记 WARNING +
  `prefix_mismatch` 事件，提醒维护名单。`rotate` 仍拒绝「IP 未变」（ISP 重发同址）。
  测试 `test_quota_cordon_lifted_for_out_of_prefix_ip_change` /
  `test_prefix_violation_accepted_and_flagged`。
- **fix(failover): 熔断闭合加迟滞，抑制抖动** — 单次主链路成功即 `FALLBACK
  RECOVERED`、单次失败即 `FALLBACK ENGAGED`，主链路约一半成功率时按请求速率来回
  切换（线上单日 57 engaged / 11 recovered，20 分钟内 6 次往返），每次切换都把
  客户端正在用的模型换成备用网关的 ID。改为连续 N 次成功才闭合
  （`ZEN_FALLBACK_RECOVER_AFTER`，默认 2），同一轮故障内的再次跳闸记为 INFO
  `FALLBACK RE-ENGAGED`（事件保留 `re_trip` 字段）。备用返回错误时释放窗口的
  既有语义不变。测试 `test_recovery_needs_consecutive_successes` /
  `test_failure_resets_the_recovery_streak` /
  `test_re_trip_is_logged_as_continuation`。
- **fix(egress): 免费层 403 触发也受 30 分钟轮换下限约束** — `should_rotate_on_
  free_tier` 此前不看 `HARD_ROTATE_MIN_INTERVAL_S`，形状门若收紧会按 403 频率
  连续重启光猫；body/形状故障换 IP 无效，故与硬 429 路径统一。测试
  `test_free_tier_trigger_respects_the_rotation_floor`。

- **fix(zen): body 门是「prompt 快照新鲜度」而非「真身文本」** — hermes 端到端
  `429 insufficient balance`（备用链路 proxy_ms 计费错误原样透传）+ 主链路三桶
  同时 `429 FreeUsageLimitError`。同出口 IP、同分钟、同 bun 传输、每次新 mint
  session 的 2×2 消融定案：官方 1.19.3 客户端匿名直跑 **200**，网关请求 **429**
  （`retry-after`→UTC0）⇒ 该 IP **没有烧穿**，是 body 门伪装成配额门。UA / 工具集
  / AGENTS 附录单独都不是判别项；**system prompt 的 `Today's date:` 停在抓包当天
  就 429，刷成当天即 200**，模型名与工作区路径属同类「旧抓包重放」信号。修复：
  `zen_prompt_default.txt` 换为 2026-10-25 官方客户端实拍真身（9559B），新增
  `_render_prompt(model)` 每请求重写模型名 / UTC 当天日期 / 工作区路径，
  `_inject_marker()` 传实际发送的上游 ID。实测同桶 **3/3 200**（修复前同一请求体
  4/4 429）。同时确立 **429 归因红线**：四因同形，换 IP 只对真实 IP 配额有效，
  排障顺序 = 传输 → prompt 新鲜度 → session → IP。测试
  `test_prompt_asset_is_a_live_client_capture` /
  `test_render_prompt_binds_model_date_and_workspace` /
  `test_prompt_date_matches_client_date_string` /
  `test_body_marker_renders_requested_model_not_capture_model`。
- **fix(failover): 备用链路返回错误时立即释放保持窗口** — proxy_ms 欠费回
  `429 insufficient balance`，旧行为把后续 120s 流量全压在它上面，客户端判
  `billing/retryable:false` 放弃，而主链路 429 可能只是 body/传输/session 门误伤。
  现在 `_via_secondary` 收到错误中继（非连接失败）时原样回传、记 ERROR
  `FALLBACK FAILED` + JSONL `fallback_failed{status}`，并清零 `_fallback_until`
  让下个请求重新探测主链路；备用**不可达**仍保留熔断。测试
  `test_backup_error_relay_releases_hold_for_next_primary_probe`。
- **fix(zen): 每次上游试探重新 mint 会话（session 单次使用）** — 上游对复用会话
  一律回层 2 同款 429（同 IP：新 mint=200、立即重放=429，且该 IP 随后仍连续服务
  新 mint ≥3 发）。旧 `chat()` 每客户端请求 mint 一次贯穿三条 failover 腿 = 从
  第二腿起重放已消费会话。改为 in-loop 逐试探 `_headers()`；测试
  `test_chat_mints_fresh_session_per_upstream_attempt`。
- **fix(zen): 免费层形状门收紧为「≥4 真名内置工具」** — 1/2/3 真名全 403；4 真名
  200 且 schema/description 内容不校验；混入 1 个改名工具即 403。UA 版本 / 头名
  （含 2.0 系改名）/ `b3` / `traceparent` / `x-opencode-client` 值均不敏感。
  `ZEN_TOOLS_MODE=minimal` 注入数 2→4（`bash,edit,glob,read`）、UA 改
  `1.19.4/ai-sdk 4.0.23/bun 1.4.4`、头集对齐 1.19.4（新增
  `x-opencode-session-id` 双头、去 legacy `x-session-id` 与重复
  `x-opencode-request-id`、`x-opencode-project` 改固定 40-hex）。
- **fix(zen): 兜底耗尽的匿名 403 改回 503 `egress_unavailable`** — 出口级 403
  原样透传会被客户端判 `auth/retryable:false`；轮换约 90s 后全池即可恢复。非门槛
  403 与带 key 模式仍按请求级透传。`zen_direct._egress_gate_answer`。
- **fix(stream): SSE 停顿看门狗 + 租约按结局释放** — 死隧道令流零字节静默满 300s
  读超时，异常穿透 `StreamRelay` 掐断客户端 SSE（hermes `stream_drop`）。
  `STREAM_STALL_S`（默认 60s）超时或中途传输异常 → 合成
  `finish_reason:"length"` + `[DONE]`；关闭钩子携带结局，`stalled`/`interrupted`
  按失败释放（3 连败 cordon），`cut` 与客户端断连仍按 ok。
- **fix(stream): 客户端写停滞看门狗** — `CLIENT_STALL_S`（默认 60s）：生成器停在
  `yield` = ASGI 写被背压阻塞，超阈值即判客户端段死 → WARNING
  `client write stalled`、取消响应任务、释放上游并写终态。监视任务自行完成释放
  与日志（被取消的响应任务会让生成器成环，依赖 close 会拖到 GC）。
- **fix(deploy): 容器启动补 `setup_logging()`** — Dockerfile 走
  `uvicorn --factory`，从未经过 `python -m proxy_opencode`，容器根 logger 无
  handler：全部 INFO 被丢弃、WARNING 走 lastResort 无 extras，无法归因到桶。
- **fix(log): httpx 访问日志静默（红线）** — httpx INFO 行打印完整 URL，池探针
  URL 的 userinfo 携带 agent Basic 凭据 → 泄入容器日志。`setup_logging` 将
  `httpx`/`httpcore` 压到 WARNING，不得回退（
  `test_setup_logging_silences_httpx_access_lines`）。
- **fix(egress): 配额/门槛 cordon 随 ISP 换 IP 解除** — cordon 是对出口 IP 的
  判决：探针观测到 IP 变化、新 IP 在 `expect_prefix` 内且就绪 → `cordon_lifted`；
  外前缀 IP、探针未就绪、transport streak cordon 不解除。
- **fix(egress): `POST /rotate` 传输失败不再立即判失败** — 命令可能已送达或 ONT
  自行重拨；照常在 deadline 内轮询，观测到合法新 IP 即按成功收敛，明确 HTTP 拒绝
  才是最终失败。
- **fix(egress): 僵尸桶 `ready` 语义（两层防御）** — agent `/healthz` 的 `ready`
  曾硬编码 true，出口全灭仍答 ready。agent 端改为
  `egress_probe_ok is not False and 探针新鲜`（`PROBE_READY_GRACE_S=45s`）；网关
  `_probe_one` 另把 `egress_probe_ok is False` 直接判 unhealthy。
- **fix(egress): 选桶轮询均摊** — 原 `min(inflight, name)` 在全空闲时恒取字母序
  最前的桶（顺序请求全落单桶）；改为 `itertools.count()` 轮转 + 稳定
  least-inflight。
- **fix(egress): 免费层 403 `FreeTierError` 按出口 IP 处理** — 释放为
  `free_tier_rejected`（失败）+ 立即 cordon + 绕过阈值换 IP（每窗口每桶一次，
  共用失败冷却）。此前按 ok 释放 → 桶一直"健康"地返回 403。
- **fix(relay): 上游空错误体补标准信封** — 上游偶发 `400 {}`，裸 dict 中继后
  掩码层又注入 `model`，客户端只看到 `400 {"model": ...}`（SDK 显示
  "malformed"）。规则：带 `error` 键原样中继，其余包成 OpenAI 错误信封（裸
  detail/message 截断 200 字符保留）。
- **fix(relay): 静默上游截断合成 `finish=length`** — 显式告知输出上限停止，
  而非协议违规。

### Added

- **feat(failover): zen-direct 出口级故障自动切备用链路 `proxy_ms`** — 主链路撞
  429/5xx/503 出口闸门/传输失败时改由同机 proxy_ms（ModelScope 聚合网关）服务，
  `ZEN_FALLBACK_HOLD_S`（默认 120s）后半开探测切回；请求级 4xx 不切换。实现
  `upstreams/failover.py` 的 `FailoverAdapter`（覆盖三个协议路由 + `/v1/models`），
  `ZEN_FALLBACK_BASE_URL` 空则不包装。三处提醒：WARNING/ERROR 日志、JSONL
  `fallback_*`、`/healthz` 的 `fallback` 块。测试 `tests/test_failover.py` 12 例；
  ECS 实弹非流式/流式均正常。
- **feat(transport): zen 上游一跳改走 bun 子进程** — 上游按 TLS/HTTP 栈判定：
  同一新出口 IP 分钟级对照，bun（boringssl/HTTP2）字节级克隆重放 200、httpx
  （OpenSSL/HTTP1.1）同字节 429（伪装成配额）。实现
  `zen_bun/forward.ts`（stdin body → stdout SSE 直通、stderr 单行
  `META {status,headers}`、hop-by-hop 头剥离、`AbortController` 超时）+
  `upstreams/zen_bun_transport.py`（spawn/环境净化/代理注入/超时/kill + httpx
  响应面鸭子类型）；`zen_direct._exchange` 分发器（bun 优先、httpx 回退，语义与
  日志一致）；镜像预装 bun 1.4.4。`ZEN_BUN_TRANSPORT/ZEN_BUN_BIN/ZEN_BUN_SCRIPT`；
  dev 无 bun 自动回退。测试 `tests/test_zen_bun_transport.py` 13 例。
- **feat(egress): 硬 429 换 IP 30 分钟下限** — `ZEN_EGRESS_ROTATE_MIN_INTERVAL_S`
  （默认 1800，0=关闭），只在**上一次换 IP 成功**后生效（失败重试仍走 15 分钟
  cooldown）。动机：429 四因同形，只有真实配额能靠新 IP 解决；2026-10-25 把非
  配额 429 当配额，单日轮换 32/20/13 次并打散隧道。
- **feat(egress): `onts[].interface` 接受字面 IPv4** — `SIOCGIFADDR` ioctl 只存在
  于 Linux，Windows/Android 写接口名会被静默忽略（两台 ONT 同为
  `192.168.1.1` 时会打错光猫）。新增 `egress.source_address()`（接口名走 ioctl，
  失败回退字面 IPv4）；测试 `test_source_address_literal_ipv4` /
  `test_ont_bind_source_accepts_literal_ipv4`。
- **feat(egress): owin10 单设备双桶接入出口池** — `10001` 有线移动
  （`bind_source 192.168.1.222`）、`10002` 电信 WiFi（`bind_source
  192.168.2.235`），Python 直跑于 `C:\egress-agent`，Task Scheduler `ONSTART`
  SYSTEM 自启；两桶均接 ONT（`huawei` / `huawei_ct`）并实弹轮换成功。
- **feat(egress): Windows 桶部署路径** — `deploy/windows/README.md`（schtasks +
  `bind_source`，含 `win10` 用 `.venv` + Startup 的旧路径对比）。
- **feat(egress): 出口池自动换 IP（P5）** — 每桶日窗 `[08:00, 08:00+1d)`
  Asia/Shanghai，达 `ZEN_EGRESS_ROTATION` 阈值触发；硬 429（`Retry-After >
  3600s` 或无该头）与 403 门槛绕过阈值。编排：标记 rotating → `/rotate` → 轮询
  IP 变化且在前缀内 → 清 cordon、计数归零；计数快照落 `/data` 跨重启存活。
  `/admin/egresses` 增 `counters` 块与手动 `POST /admin/egresses/{name}/rotate`。
- **feat(egress): 结构化事件日志** — `egress/eventlog.py`（JSONL，5MB×2 轮转，
  写失败自动禁用），记录 cordon / 换 IP 前后 IP / 探测到的 IP 变化 /
  `window_rollover` / `free_catalog_refreshed` / `fallback_*`。
- **feat(stream): `STREAM_STALL_S` / `CLIENT_STALL_S` 配置项**（默认 60s，0=关闭；
  事实源 `proxy_opencode/config.py`）。

### Changed

- **chore(egress): 出口池收敛为 owin10 双桶 + cc9** — `win10` 移出（与
  owin10-mobile 同 CGNAT 出口、无独立换 IP 能力），`opc2` 两桶随设备永久下线
  移除；`EXPECT_PREFIX` 补当日实测落段（telecom `121.234.0.0/16`、cc9
  `122.195.0.0/16`、mobile `36.149.0.0/16`），否则换 IP 后 cordon 解不开。
- **chore(zen): 公开模型目录回笼单别名** — 撤第二别名 `opencode/big-pickle`，
  `PUBLIC_MODELS=deepseek-ai/deepseek-v4.1-flash:DeepSeek V4.1 Flash:big-pickle`
  （备份 `.env.bak-singlemodel`）。
- **chore(ops): owin10 Hermes 客户端 provider 收敛为 `oc_uni`** — `openai-api` 是
  Hermes 内置 provider，且 `auth.json` 的 `credential_pool` 会被 backing source
  反复播种（几分钟内复活）。三步清理见 `docs/OPERATIONS.md` §2.2。
- **docs: 文档整体浓缩（本版）** — 同一事实只在一处维护：`ARCHITECTURE.md` 只留
  拓扑/端口/池语义/ADR，`OPERATIONS.md` 只留现状/操作/排障/风险，事故编年压缩为
  表格，`EGRESS_OBSERVABILITY.md` 区分「已落地的决策事件流」与「待落地的逐请求
  账本」，`CHANGELOG` 合并重复小节。

### Docs

- **docs: GitHub 访问方式（`ACCESS.md` §6）** — 远端
  `git@github.com:gitychzh/proxy_opencode.git`，统一 SSH over 443
  （`ssh.github.com:443`，国内 22 端口常被阻断）、`~/.ssh/config` 模板、各机私钥
  归属与工作副本路径、`ssh -T` 验证口径。`gh` CLI 未安装，不要依赖 `gh auth`。
- **docs: Egress 健康判定口径统一** — `healthy = ready 且探针 200`；
  `matches_prefix` 仅作审计字段，不再影响健康。

### Removed

- 旧栈残留：ECS 上的 `zen-lb` / `zen-glm53` / `cloudflared-ingress`（2026-10-17
  清退）、ECS 本机桶（机房 IP 额度即耗）、opc2 双桶（设备永久下线）。
- 文档中的三桶/四桶历史拓扑、"两栈并存"、双桶性能对比与抓包复盘细节（压缩为
  `ARCHITECTURE.md` §10 与 `OPERATIONS.md` §5 的要点）。
