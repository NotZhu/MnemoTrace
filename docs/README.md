# MnemoTrace 文档

MnemoTrace 是面向 LLM 智能体的开源、自托管记忆引擎。本索引按“先跑通、再接入、再运维或维护”组织；详细指南目前以简体中文撰写，欢迎贡献英文翻译。

[English README](../README.md) · 简体中文

> 文档跟随 `main` 分支；版本与兼容性说明见[项目状态](../README.zh-CN.md#项目状态)。

## 第一次使用

1. 按[快速开始](../README.zh-CN.md#快速开始)启动服务、签发 API 密钥，并跑通一次写入和召回。
2. 用 [HTTP API 接入指南](reference/http-api.md) 走完会话创建、异步写入、任务轮询和证据召回。
3. 读[记忆引擎架构](architecture/memory.md)，理解空间、会话、事实、证据组和双时间轴。

## 按任务选择

### 接入应用

- **调用 HTTP**：处理异步写入、幂等恢复、召回和分页。见 [HTTP API v1 接入指南](reference/http-api.md)。
- **使用 TypeScript SDK**：在 Node.js 中写入记忆并回查证据。见 [TypeScript SDK 指南](../clients/typescript/README.md)。

### 自托管运维

- **部署与故障处理**：启动服务、备份恢复、密钥轮换和排查。见 [自托管部署与运维](../deploy/README.md)。
- **配置模型**：选择补全输出模式、切换嵌入版本或启用重排。见 [模型配置参考](reference/model-configuration.md)。

### 数据管理

- **删除或擦除数据**：删除事实、擦除会话、清空或删除空间，并核对结果。见 [数据生命周期指南](guides/data-lifecycle.md)。

### 引擎维护与评测

- **理解引擎**：事实时间轴、证据组、写入与召回流程、擦除边界。见[记忆引擎架构](architecture/memory.md)；测试入口见[验证依据](architecture/memory.md#验证依据)。
- **运行评测**：数据集、命令、指标和发布校验。见[评测指南](../eval/README.md)。

## 阅读约定

- 命令默认从仓库根目录运行；示例使用合成数据，不包含真实凭据。
- HTTP 契约以 `GET /openapi.json` 为准；字段和错误码以实际响应为准。
- 文档按目录组织：`architecture/` 说明概念与一致性，`guides/` 提供任务步骤，`reference/` 记录契约字段。
- 新增或移动页面时，同步更新本索引。

## 参与项目

- 提交问题或变更：[贡献指南](../CONTRIBUTING.md)
- 报告漏洞：[安全政策](../SECURITY.md)
- 查看用户可见变化和兼容性影响：[变更日志](../CHANGELOG.md)
- 开发规范与验证入口：[AGENTS.md](../AGENTS.md)
