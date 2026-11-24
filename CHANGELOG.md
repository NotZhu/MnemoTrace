# 变更日志

本文件记录 MnemoTrace 各版本中影响使用或升级的变更。格式采用 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。

## Unreleased

### Added

- 新增长期记忆评测套件，覆盖 LongMemEval-S、LoCoMo 和 PersonaMem v2，支持同次运行的全文输入和 BM25 基线、逐题记录、断点续跑与发布校验。 [评测指南](eval/README.md)

### Changed

- Python 分发包和导入名统一为 `mnemotrace`。部署采用 `deploy/compose.yaml` 和 `MNEMOTRACE_*` 配置。 [快速开始](README.zh-CN.md#快速开始)
- **破坏性变更（数据库）** 新 PostgreSQL 基线要求从空数据库初始化，旧库迁移暂不受支持。
- **破坏性变更（自定义模型适配器）** `complete_json` 需要接受 `parse` 关键字参数，并在提供该参数时返回解析后的结果。

### Fixed

- 结构化事实抽取遇到领域校验错误时，纳入纠错重试，与 JSON 和 Schema 校验共享请求预算。
