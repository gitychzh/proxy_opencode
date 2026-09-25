# 使用 hermes 做合规端到端测试

本说明演示如何把本机的 hermes 指向本地网关 `http://127.0.0.1:8787/v1`，
对上游做一次正常的、合规的端到端调用。这里不涉及任何风控绕过、
免费额度滥用或指纹伪造——网关只是把已授权的请求转发到你拥有凭据的上游。

## 1. 配置并启动网关

准备一个上游（例如你自己的 OpenAI-compatible 服务）的合法 API key，然后：

```bash
cd D:\vs_ps\p1\proxy_opencode
pip install -e .

export UPSTREAM_BASE_URL="https://api.openai.com"   # 或你的自建兼容服务
export UPSTREAM_API_KEY="sk-...."                    # 你对上游的合法 key
export GATEWAY_API_KEYS="local-dev-key"              # 本机客户端使用的网关 key
export REASONING_PASSTHROUGH=true
export PORT=8787

uvicorn proxy_opencode.app:app --host 127.0.0.1 --port 8787
```

健康检查：

```bash
curl http://127.0.0.1:8787/healthz
```

## 2. 先用手动请求验证网关

```bash
# 模型列表
curl http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer local-dev-key"

# 非流式补全
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

预期：上游正常应答，网关日志只有 request id / model / status / latency / usage，
不含消息内容与 key。

## 3. 配置 hermes 指向网关

hermes 支持 OpenAI-compatible base URL 的配置方式（环境变量或配置文件，
以你本机 hermes 的版本为准），例如：

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8787/v1"
export OPENAI_API_KEY="local-dev-key"   # 网关 key，不是上游 key
```

然后正常使用 hermes 发起一次对话请求。整条链路为：
hermes → 本地网关（鉴权/限流/日志）→ 上游（合法 key 正常计费/计量）。

## 4. 验证要点

- hermes 不持有也永远拿不到 `UPSTREAM_API_KEY`；它只使用网关 key。
- 网关日志中可看到对应请求记录，且无任何消息内容或 key 泄露。
- 超过 `REQUESTS_PER_MINUTE` 会得到 OpenAI 风格的 429 错误。
- 若上游拒绝请求，差错以原始状态码和 OpenAI 风格 JSON 原样返回给 hermes。

## 5. 停止

`Ctrl+C` 停止 uvicorn。本测试不修改任何远程服务的状态。

## 6. 无真实上游 key 时：先用 fake upstream 验证 hermes→gateway 链路

还没有可用上游 key 时，可以用 `tests/fake_upstream.py` 充当上游，先把
hermes 到网关的链路、鉴权与流式行为验证通：

```bash
cd D:\vs_ps\p1\proxy_opencode

# 终端 1：fake upstream（完全本地，无需任何真实 key）
python tests/fake_upstream.py --port 9901

# 终端 2：网关指向上面的 fake upstream（全部用 dummy key）
export UPSTREAM_BASE_URL="http://127.0.0.1:9901"
export UPSTREAM_API_KEY="upstream-dummy-key"
export GATEWAY_API_KEYS="local-dev-key"
uvicorn proxy_opencode.app:app --host 127.0.0.1 --port 8787

# 终端 3（hermes 侧）：只设置两个环境变量
export OPENAI_BASE_URL="http://127.0.0.1:8787/v1"
export OPENAI_API_KEY="local-dev-key"     # 网关 key
```

hermes 中选择 OpenAI 兼容模型名 `fake-openai-model`（fake upstream 的
`/v1/models` 返回的模型 id），发起一次普通对话。

成功判定：

- hermes 正常收到回复，内容为 `fake upstream reply`（流式时为分段增量）。
- 网关日志出现对应请求记录（request_id / model / status / latency / usage）。
- fake upstream 响应的 `metadata.echo` 中 `authorization_sha256_12` 对应
  `UPSTREAM_API_KEY`（dummy）而非 hermes 持有的网关 key；hermes 全程
  只需要也仅能使用 `OPENAI_API_KEY=local-dev-key`。

失败判定：

- hermes 报 401：网关 key 没配对（检查 `OPENAI_API_KEY` 与
  `GATEWAY_API_KEYS`）。
- 报 429：触发限流，降低频率或调大 `REQUESTS_PER_MINUTE`。
- 报 502：fake upstream 没启动或 `UPSTREAM_BASE_URL` 指向错误。
- 其它上游风格错误会以原始状态码透传给 hermes。

验证通过后，把 `UPSTREAM_BASE_URL`/`UPSTREAM_API_KEY` 换成真实上游的
合法凭据，hermes 侧配置不变，即可进入真实 e2e。
