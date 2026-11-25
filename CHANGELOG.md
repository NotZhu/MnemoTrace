# 变更日志

本文件记录 MnemoTrace 各版本中影响使用或升级的变更。格式采用 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。

## Unreleased

### Added

- 新增长期记忆评测套件，覆盖 LongMemEval-S、LoCoMo 和 PersonaMem v2，支持同次运行的全文输入和 BM25 基线、逐题记录、断点续跑与发布校验。 [评测指南](eval/README.md)

### Changed

- Python 分发包和导入名统一为 `mnemotrace`。部署采用 `deploy/compose.yaml` 和 `MNEMOTRACE_*` 配置。 [快速开始](README.zh-CN.md#快速开始)
- **数据库基线** PostgreSQL 从空数据库初始化，空间表包含发布复核所需的 `fact_revision`。 [部署说明](deploy/README.md#停机备份与升级)
- **破坏性变更（数据库配置）** 内置 Compose 只需填写 `MNEMOTRACE_DB_PASSWORD`，各进程统一读取 `MNEMOTRACE_POSTGRES_DSN`。当前基线使用空卷初始化应用账号和 schema。已有四凭据部署使用独立的 Compose 项目试用。 [部署说明](deploy/README.md)
- **破坏性变更（自定义模型适配器）** `complete_json` 需要接受 `parse` 关键字参数，并在提供该参数时返回解析后的结果。
- 启用补全须声明 `MNEMOTRACE_LLM_CONTEXT_WINDOW_TOKENS`。自定义模型适配器须实现 `structured_prompt_fits` 并支持 `complete_json` 的 `enforce_context_budget` 参数。 [模型配置](docs/reference/model-configuration.md)
- `InferenceRequestError` 使用本地生成的 `attempt_id`。自定义存储的 `SpaceReader` 须提供与快照绑定的 `get_fact_revision`。
- 写入在空间写锁外检索候选，版本变化后最多重查一次。运行连接设置锁等待和语句时限。 [写入流程](docs/architecture/memory.md#写入运行视图) · [部署配置](deploy/README.md#配置)

### Fixed

- 结构化事实抽取遇到领域校验错误时，纳入纠错重试，与 JSON 和 Schema 校验共享请求预算。
