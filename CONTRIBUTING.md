# 贡献指南

欢迎为 MnemoTrace 提交缺陷修复、功能改进、测试和文档。

## 报告问题和提出建议

- 报告缺陷时，使用[缺陷报告表单](https://github.com/NotZhu/MnemoTrace/issues/new/choose)。提供 MnemoTrace 版本或 commit、运行环境、预期与实际结果，以及使用合成数据的最小复现。附带日志或截图时请先脱敏。
- 提出功能建议时，说明使用场景、遇到的限制和期望行为。涉及公开接口、存储契约或部署兼容性的取舍，建议先在 Issue 中讨论。
- 安全漏洞请通过[安全政策](SECURITY.md)列出的方式报告。

## 本地开发与验证

- 按[验证与评测](AGENTS.md#验证与评测)选择对应的检查。后端完整检查命令见[开发验证](README.zh-CN.md#开发验证)。
- 修改公开用法、接口契约或部署行为时，更新[文档索引](docs/README.md)对应的说明。对用户有影响的变化可记入[未发布变更](CHANGELOG.md#unreleased)。

## 提交 Pull Request

1. 从 `main` 创建功能分支，按[协作与交付规范](AGENTS.md#协作与交付)命名分支和提交。让每个 PR 聚焦一个问题或一组相关改动。
2. 使用符合协作规范的 PR 标题，例如 `fix(recall): 修复历史证据的筛选`。
3. 按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md)填写问题、变更和验证结果，并附 CI 运行链接。
