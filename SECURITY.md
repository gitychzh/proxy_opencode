# Security Policy

## 支持的版本

只有 `main` 分支上的最新发布（`v*` tag）接收安全修复。历史版本不提供回溯补丁。

| 版本 | 是否支持 |
|---|---|
| 最新 `v*` 发布 | ✅ |
| 更早版本 | ❌ |

## 报告漏洞

**不要通过公开 GitHub issue 报告安全漏洞。**

请通过 GitHub 的 Private vulnerability reporting（仓库 Security 标签页 →
"Report a vulnerability"）私下提交，包含：影响描述、最小复现（使用 dummy
key）、受影响版本。我们会在确认后尽快回复并协调修复与披露时间。

## 密钥处理

- 任何 API key / 凭证都只能通过环境变量（如 `UPSTREAM_API_KEY`、
  `GATEWAY_API_KEYS`）注入，**永不入库**，测试与文档同样只使用 dummy key。
- 日志只记录元数据（`request_id`、`model`、`status`、`latency_ms` 等），
  **绝不记录消息体内容或任何 API key**。
- 客户端网关 key 不透传给上游；若发现透传路径，请按漏洞上报。

## 不接受的安全请求

本项目**不接收**以下类型的报告或功能请求，将直接关闭：

- 绕过上游服务风控 / 限频 / 地域限制
- 滥用免费额度或对免费模型进行批量压榨
- 客户端指纹伪造、反检测
- 获取、使用或转发他人的真实上游 key

该边界与 README 合规声明及 `docs/engineering-constraints.md` 一致。
