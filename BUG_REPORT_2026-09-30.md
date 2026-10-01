# proxy_opencode 整体 BUG 排查报告

日期：2026-09-30 · 版本：v0.6.0 (1b1d76b) · 范围：全部源码（约 4300 行）+ balancer

**基线状态**：`pytest tests/ balancer/tests/ -q` → 103 passed；`ruff check .` → 0 告警。
以下问题均为现有测试未覆盖的边界缺陷，按严重程度分级。

---

## 高优先级（协议正确性，实际客户端会踩到）

### H1. `/v1/responses` 流式：output_index 冲突
- **位置**：`proxy_opencode/formats/responses_proto.py` `_StreamState`
- **现象**：`next_item_index` 初始为 0，而文本消息项固定使用 `msg_index = 0`。
  当响应**同时含文本和 tool_calls**（codex CLI 常态）时，两个
  `response.output_item.added` 事件的 `output_index` 均为 0（已写脚本复现确认）。
  codex CLI 按 output_index 关联 delta 与 item，重复索引会导致
  item 状态错乱 / delta 串扰。
- **修复建议**：工具项的 `item_index` 改由统一分配器生成；若文本项已占用 0，
  则从 1 开始。

### H2. `/v1/messages`：tool_result 与 text 混排时违反 OpenAI 消息顺序契约
- **位置**：`proxy_opencode/formats/anthropic_proto.py` `_blocks_to_messages`
- **现象**：Claude Code 在工具结果后附带文本是常态
  （user content = `[tool_result, text]`）。当前实现把
  `role=user` 文本消息放在 `role=tool` 消息**之前**（已复现确认），
  产出顺序 `assistant(tool_calls) → user(text) → tool(result)`，
  违反 "tool 消息必须紧跟带 tool_calls 的 assistant 消息" 的 OpenAI 契约。
  严格校验的上游（OpenAI 官方、多数聚合网关）会直接 400。
- **修复建议**：非 assistant 角色的块列表中，tool 消息先 extend、文本消息最后 append。
- **备注**：现有测试只覆盖了"纯 tool_result 无文本"用例
  （`tests/test_anthropic_api.py` 断言 `["user","assistant","tool"]`），未覆盖混排。

## 中优先级（API 语义 / 可观测性）

### M1. `/admin/keys` 校验错误返回 HTTP 200
- **位置**：`proxy_opencode/routes/admin.py` L36-48、L66-73
- **现象**：`ttl_hours` 非法、key_id 不存在时返回 `{"error": {...}}` 但状态码 200，
  客户端（curl 脚本、CI）无法用状态码判错，与仓库其它路由的错误风格不一致。
- **修复建议**：返回 400/404 状态码（可用 `openai_error()`）。

### M2. responses / anthropic 路由 ServeCompletion 路径重复记日志
- **位置**：`routes/responses_api.py` L125-139、`routes/anthropic_api.py` L164-176
- **现象**：`log(200, usage)` 与后续 `log(200)` 各执行一次，同一请求产生两条
  "completion" 日志（chat 路由无此问题），统计口径被放大。

### M3. 流式生命周期日志只覆盖 chat 路由
- **位置**：`routes/responses_api.py`、`routes/anthropic_api.py` 流式分支
- **现象**：README 承诺的 `stream ended`（chunks/ttfb/duration/reason）仅在
  `chat.py` 的 `_relay` 中实现；另两个协议路由的流式请求结束/断连/出错
  无任何终止记录，"慢与死可区分"的可观测性对 codex / claude code 不成立。
- **修复建议**：将 `_relay` 提为共享包装（在协议转换层外再包一层字节流日志）。

### M4. Responses 流式完全丢弃 reasoning_content
- **位置**：`formats/responses_proto.py` `stream_responses_events`
- **现象**：非流式路径会产出 `{"type":"reasoning"}` 输出项，但流式路径对
  `delta.reasoning_content` 无任何处理，codex 收到的流与聚合结果不一致。

## 低优先级（健壮性 / 加固）

### L1. RateLimiter._hits 条目永不回收
- **位置**：`proxy_opencode/ratelimit.py`
- **现象**：按 token 的滑动窗口 deque 永不删除；因仅在鉴权通过后调用，
  条目数 ≤ 历史有效 key 数，增长缓慢但长期驻留进程不释放。

### L2. balancer `/healthz` 未鉴权暴露内部拓扑
- **位置**：`balancer/lb.py` create_app：`/healthz` 检查在 LB_KEY 校验之前
- **现象**：绑定 `0.0.0.0` 时局域网任意主机可读取各桶 target、last_error
  （可能含上游 429 响应体片段）、请求量等。建议 healthz 只回 `healthy` 布尔，
  详细 snapshot 挪到鉴权后的 `/admin/json`。

### L3. balancer 客户端断连时仍转发截断的请求体
- **位置**：`balancer/lb.py` `read_body`：`http.disconnect` 时 break 后照常转发
- **现象**：半截 body 上送上游，可能产生一次"幽灵请求"并消耗免费额度。
  应直接中止本次请求。

### L4. LB 客户端断连把健康桶计为失败
- **位置**：`balancer/lb.py` 流式中途异常路径：`release(up, False, ...)`
- **现象**：客户端主动断连（常见）会被计入 `consecutive_failures`，
  连续 3 次可把健康桶错误标记为 down。建议区分 `streamed=True` 的断连不计失败。

### L5. `openai_http.list_models` 200 但非 JSON 时未捕获
- **位置**：`upstreams/openai_http.py` L63-67 + `routes/models.py`
- **现象**：`resp.json()` 抛出的 ValueError 不在路由的
  `except (ConnectionError, httpx.HTTPError)` 内 → 网关 500。

### L6. KeyStore 写盘为同步 IO，跑在事件循环线程
- **位置**：`auth/keystore.py` `_save`（create/revoke 时调用）
- **现象**：JSON 落盘阻塞事件循环；key 量小可接受，量大或磁盘慢时造成微卡顿。

### L7. `/admin?key=...` 查询串鉴权
- **位置**：`balancer/lb.py`
- **现象**：LB key 出现在 URL 中，会落入浏览器历史 / 反代访问日志。
  建议 Cookie 或仅 Bearer。

## 已排查、确认无问题（记录以免重复踩坑）

- `sse_mask` 正则**不会**误改写内容文本或 tool arguments 中的 `"model":`
  （JSON 转义使内层 `"model\"` 无法命中正则；已写用例验证，顶层级 model 字段正确改写）。
- serve 适配器 `DELETE /session/{sid}` 与其它 `/api/*` 前缀不一致，
  但与模块文档及测试 mock 一致（live 验证过），非 bug。
- `_aggregate_sse` 的 tool_call 分片合并、`[DONE]` / 非 JSON 尾行容错正确。
- KeyStore 原子写（mkstemp + os.replace）在 Windows 下语义正确。
- zen-direct 免费层三要素注入、host 白名单、serve loopback 校验逻辑严密。
- 认证失败日志只记 `provided: bool`，无凭据泄漏；日志白名单机制有效。

## 建议的修复顺序

1. H1、H2（协议正确性，codex/claude code 实际使用者会踩）
2. M1（破坏脚本化调用的判错）
3. M2/M3/M4（可观测性与一致性，可合并一个小 PR）
4. L1-L7（择机加固）

每项建议补一条对应用例：H1（流式文本+tool_calls 事件序列）、
H2（tool_result+text 混排）、M1（admin 错误状态码）。
