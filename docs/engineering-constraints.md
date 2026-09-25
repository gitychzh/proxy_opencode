# 工程化约束（长期维护基线）

本项目从创建起按“可长期维护、可审计、可回滚”的大公司流程治理。以下为强制约束，后续 PR/变更不得违背；如需调整，必须先改本文件并在 PR 中说明原因。

## 语言与运行时

- Python 固定为 **3.12.13**；`pyproject.toml` 使用 `requires-python = "==3.12.13"`，CI 与本地开发都必须使用该版本。
- Node.js 仅在需要前端/脚本工具链时使用，并始终使用**最新 LTS/Stable**；若引入 Node，必须提交 `.nvmrc` 与 lockfile，禁止隐式依赖本机全局版本。
- 不允许引入未经评估的依赖；优先使用成熟第三方库（FastAPI/httpx/uvicorn/pytest 等），核心代理逻辑保持小而清晰。

## 合规边界

- 本项目只做授权上游的协议转发、鉴权、限流、日志与观测。
- 禁止实现或协助：风控绕过、免费额度滥用、客户端指纹伪造、共享/窃取凭证、抓取或保存浏览器 Cookie/Session。
- 上游密钥只能存于服务端环境变量；客户端只持有网关 key。

## 模块化与代码结构

- `proxy_opencode/` 为运行时代码：`config`（环境配置）、`ratelimit`（限流）、`app`（HTTP 层）等模块职责单一；新增能力优先新建小模块而不是堆进 `app.py`。
- `tests/` 为 pytest 测试；任何行为变更必须补/改测试。
- `scripts/` 放运维与端到端说明；不放一次性临时脚本。
- 环境变量是唯一配置来源；禁止硬编码 key、上游地址、模型名到代码。

## Git 与版本控制

- 全部变更通过 Git 管理；主分支 `main` 受保护，禁止裸 force-push，除非仓库重建且有备份。
- 分支模型：`feature/*` 开发，`fix/*` 修复，`release/*` 准备发版；合并用 PR，要求 CI 通过。
- 提交信息采用 Conventional Commits：`feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:`。
- 发版使用语义化版本 SemVer 与 tag：如 `v0.2.0`；REAKING 变更必须升级 major 并在 CHANGELOG 说明。
- 远程重写/删除分支/tag 前必须已有 mirror 备份，并在 PR/issue 留下备份路径。

## 质量门禁

- 必跑：`python -m pytest tests/ -q`。
- 建议逐步接入：`ruff`/`black`（格式化与静态检查）、`mypy`（类型）、`pip-audit`/`safety`（依赖漏洞）、GitHub Actions CI。
- 日志只允许记录元数据（request_id/model/status/latency/usage），禁止记录消息体、key、敏感头。

## 安全与运维

- 密钥不入库、不进浏览器持久化、不进聊天记录；使用环境变量或系统密钥管理。
- 变更上游协议、鉴权、限流、日志字段时，同步更新 README 与本文件。
- 每次发布前做 smoke：健康检查、非流式、流式 SSE、tools、reasoning 透传、429 限流。

## 当前基线

- Python：3.12.13
- Node.js：最新 LTS/Stable（引入时锁定）
- 默认上游：`https://api.openai.com`（可配置）
- 测试基线：`tests/` 全绿后方可合入/发版
