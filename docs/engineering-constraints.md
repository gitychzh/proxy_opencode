# 工程化约束（长期维护基线）

1. **Python 固定 3.12.13**：`pyproject.toml` 的 `requires-python` 是唯一事实；
   升级需 PR + CI 绿。
2. **全程 Git + Conventional Commits**：功能 `feat:`、修复 `fix:`、文档
   `docs:`、重构 `refactor:`；合入 main 前 `pytest tests/` 必须全绿。
3. **语义化版本**：`proxy_opencode/__init__.py` 与 `CHANGELOG.md` 同步；
   `v*` tag 触发 release workflow。
4. **测试基线**：新增/修改行为必须带测试（单元级用 respx 伪 serve，不依赖
   真实 opencode；端到端用 `scripts/e2e_hermes.py`，需本机环境，不进 CI）。
5. **合规边界**：不实现风控绕过、额度放大、客户端指纹伪造、对外分发凭证；
   免费额度与限制完全由官方 opencode 服务端控制（见 AGENTS.md）。
6. **绝不记录**：消息内容、prompt、API key。日志只留
   `request_id/model/stream/has_tools/status/latency_ms/usage`。
7. **Node.js**：当前未引入；若引入，锁定 LTS 并提交 lockfile。
