## 变更类型

<!-- 勾选所有适用项 -->

- [ ] Bug fix（非破坏性修复）
- [ ] New feature（非破坏性新增）
- [ ] Breaking change（破坏兼容的变更）
- [ ] Docs / 文档
- [ ] Chore（构建、CI、依赖等）

## 变更说明

<!-- 做了什么、为什么这么做，关联 issue 用 Closes #xx -->

## Testing

<!-- 必须完成下列两项，除非有充分理由并经 reviewer 同意 -->

- [ ] `python -m pytest tests/ -q` 全部通过
- [ ] `bash scripts/smoke_local.sh` 全部通过
- [ ] 新增行为有对应测试用例

输出摘要（可粘贴最后几行）：

```
```

## 安全与合规确认

- [ ] 无任何 API key / 密钥 / 凭证入库（含测试与文档）
- [ ] 未新增消息体内容日志（日志只含元数据，`messages` 内容绝不落盘）
- [ ] 未引入风控绕过、免费额度滥用、客户端指纹伪造等能力
- [ ] 客户端网关 key 未透传给上游

## CHANGELOG

- [ ] 用户可见变更已更新 `CHANGELOG.md`（或本 PR 无用户可见变更）
