# 变更日志

本文件记录影响使用、升级或集成的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本号使用 `MAJOR.MINOR.PATCH`（SemVer）。1.0 之前不承诺向后兼容；破坏性变更会明确标注，并链接到升级说明。内部重构和文档修正不列入。

> **版本说明：** MnemoTrace 尚未发布；当前开发版本为 `0.1.0`。`Unreleased` 汇总首个版本前的变更。

## Unreleased

首个 MnemoTrace 版本将包含 HTTP v1、异步写入与故障恢复、双时间轴事实、混合召回、用户画像、会话摘要、TypeScript SDK、长期记忆评测套件和 Compose 部署。以下按变更类型列出具体条目。

### Added

- 写入回执、任务详情和任务列表返回 `attempts`，表示 worker 成功领取任务的次数。 [HTTP API 接入指南](docs/reference/http-api.md#等待任务完成)
- 内置 Compose 可用 `admin bootstrap` 一次签发首把 API 密钥并创建默认空间。 [快速开始](README.zh-CN.md#快速开始)
- 新增长期记忆评测套件，覆盖 LongMemEval-S、LoCoMo 和 PersonaMem v2，支持同次运行的全文输入和 BM25 基线、逐题记录、断点续跑与发布校验。 [评测指南](eval/README.md)

### Changed

- **破坏性变更（API 密钥与默认账号）** 采用 `mnemo_` 前缀的 49 字符凭据，初始化使用默认账号 `owner`。使用新数据库基线并重新签发客户端密钥。 [API 密钥管理](deploy/README.md#api-密钥管理)
- 画像刷新按组有界读取证据，整份发布并统一结算。`GET /profile` 返回空间级刷新状态。 [画像 API](docs/reference/http-api.md#用户画像)
- **破坏性变更（包名与配置）** Python 分发包和导入名统一为 `mnemotrace`；部署采用 `deploy/compose.yaml` 和 `MNEMOTRACE_*` 配置。 [快速开始](README.zh-CN.md#快速开始)
- **破坏性变更（数据库基线）** PostgreSQL 从空数据库初始化，空间表包含发布复核所需的 `fact_revision`。 [部署说明](deploy/README.md#停机备份与升级)
- **破坏性变更（数据库配置）** 内置 Compose 只需填写 `MNEMOTRACE_DB_PASSWORD`，各进程统一读取 `MNEMOTRACE_POSTGRES_DSN`。当前基线使用空卷初始化应用账号和 schema。已有四凭据部署使用独立的 Compose 项目试用。 [部署说明](deploy/README.md)
- **破坏性变更（自定义模型适配器）** `complete_json` 需要接受 `parse` 关键字参数，并在提供该参数时返回解析后的结果。
- **破坏性变更（模型配置）** 启用补全须声明 `MNEMOTRACE_LLM_CONTEXT_WINDOW_TOKENS`；自定义模型适配器须实现 `structured_prompt_fits` 并支持 `complete_json` 的 `enforce_context_budget` 参数。 [模型配置](docs/reference/model-configuration.md)
- **破坏性变更（TypeScript SDK）** TypeScript SDK 重写为 HTTP v1 客户端，支持幂等写入、任务续查和原文追溯；调用方需传入服务地址、凭据和空间，类型要求任务响应包含 `attempts`。
- **破坏性变更（自定义适配器）** `InferenceRequestError` 使用本地生成的 `attempt_id`；自定义存储的 `SpaceReader` 须提供与快照绑定的 `get_fact_revision`。
- 写入在空间写锁外检索候选，版本变化后最多重查一次。运行连接设置锁等待和语句时限。 [写入流程](docs/architecture/memory.md#写入运行视图) · [部署配置](deploy/README.md#配置)

### Fixed

- 结构化事实抽取遇到领域校验错误时，纳入纠错重试，与 JSON 和 Schema 校验共享请求预算。
