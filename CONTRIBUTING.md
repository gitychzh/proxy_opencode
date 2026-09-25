# Contributing

本项目遵循 `docs/engineering-constraints.md` 的工程化约束。本文件是日常开发流程速查。

## 分支模型

| 分支 | 用途 |
|---|---|
| `main` | 受保护主分支，只通过 PR 合入，要求 CI 通过 |
| `feature/*` | 新功能开发 |
| `fix/*` | 缺陷修复 |
| `release/*` | 发版准备（版本号、CHANGELOG） |

禁止直接向 `main` push，禁止无备份的 force-push。

## 提交信息（Conventional Commits）

```
feat: add per-model rate limit
fix: return 502 when upstream is unreachable
docs: update README config table
refactor: split sse passthrough into module
test: cover 429 retry behaviour
chore: bump version to 0.1.0
```

type 范围：`feat` / `fix` / `docs` / `refactor` / `test` / `chore`。
BREAKING 变更在正文写 `BREAKING CHANGE:` 说明，并升级 major 版本。

## PR 检查清单

- [ ] CI 通过（install + pytest）
- [ ] 行为变更已补/改 `tests/` 中的测试
- [ ] 无密钥、无上真实 key、无消息体日志
- [ ] 涉及约束变更时同步更新 `docs/engineering-constraints.md` 并在 PR 说明原因
- [ ] 发版 PR 更新 `CHANGELOG.md` 与 `pyproject.toml` 的 `version`

## 禁止事项

- 风控绕过、免费额度滥用、客户端指纹伪造相关代码或文档
- 任何凭证/密钥/Cookie/Session 入库（含测试 fixture 中的真实凭证）
- 日志记录消息体、API key 或敏感请求头
- 硬编码上游地址、模型名、key 到代码（配置只能来自环境变量）

## 如何跑测试

```bash
pip install -e ".[test]"
pytest tests/ -q
```

要求 Python 3.12.13（见 `docs/engineering-constraints.md`）。
若本机装有 ruff/mypy，可执行 `ruff check proxy_opencode tests` 与 `mypy proxy_opencode`（配置已内置，未安装时跳过即可）。

## 版本与发版

语义化版本（SemVer）。发版通过 `release/*` 分支进行：更新版本号与 CHANGELOG 后合入并打 tag（如 `v0.1.0`）。
