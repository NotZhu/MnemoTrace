# MnemoTrace 文档

忆痕为 LLM 智能体提供跨会话记忆。当前版本为 0.1.0 Beta，支持离线使用及单节点自托管。

按要完成的任务选择入口：

| 任务 | 文档 |
| --- | --- |
| 在本机跑通写入、整合与检索 | [快速开始](quickstart.md) |
| 接入 Codex、Claude Code 或其他客户端 | [Agent 接入指南](guides/agents.md) |
| 调用 SDK 或服务 | [Python API](reference/python-api.md)、[HTTP API](reference/http-api.md)、[MCP 工具](reference/mcp.md) |
| 选择存储、模型和算法开关 | [配置参考](reference/configuration.md) |
| 部署、备份、升级或轮换密钥 | [部署说明](../deploy/README.md) |
| 理解模块与数据流 | [架构总览](architecture/README.md)、[记忆生命周期](architecture/memory.md)、[检索与上下文](architecture/retrieval.md) |
| 理解服务和界面边界 | [服务架构](architecture/service.md)、[控制台架构](architecture/console.md) |
| 查设计理由及未解决问题 | [设计取舍](decisions.md)、[问题台账](issues.md) |
| 开发和验证变更 | [贡献指南](../CONTRIBUTING.md)、[项目规则](../AGENTS.md) |
| 查看成绩或复现实验 | [公开结果](../RESULTS.md)、[评测操作](../eval/README.md)、[证据目录](../results/README.md) |

API、配置与类型以代码及对应测试为准；架构文档解释当前机制与边界。设计目标不代表实测能力，问题台账中的待验收事项不能作为已完成承诺。公开质量数字由 RESULTS 及其原始日志支撑。

`docs/index.html` 和 `assets/` 同时承担现有静态网页发布；HTML 与共享资源的维护入口见 [landing](../landing/README.md)。论文材料位于 [paper](../paper/README.md)，不承担项目工作规则。
