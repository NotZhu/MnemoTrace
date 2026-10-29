# MnemoTrace 文档

按需要完成的任务选择入口。技术正文以当前实现为基础；API 名称、命令和文件名保留原文。

## 开始使用

- [快速开始](quickstart.md)：配置模型、写入、检索与保存。
- [使用与管理记忆](guides/usage.md)：导入、事实、会话、画像、关注点和隐私操作。
- [Agent 接入](guides/agents.md)：本地与远程 MCP、配置生成、诊断和会话收尾。
- [部署与运维](../deploy/README.md)：HTTP 服务、控制台、容器、鉴权和备份恢复。

## 查询接口


| 接口或配置                | 参考                                          |
| --------------------------- | ----------------------------------------------- |
| Python 库                 | [Python API](reference/python-api.md)         |
| HTTP 协议                 | [HTTP API](reference/http-api.md)             |
| MCP 工具                  | [MCP 参考](reference/mcp.md)                  |
| TypeScript/JavaScript SDK | [SDK README](../clients/typescript/README.md) |
| 模型、运行参数与命令      | [配置参考](reference/configuration.md)        |

## 理解与开发

- [架构总览](architecture/README.md)：模块边界、读写路径和设计取舍。
- [记忆生命周期与持久化](architecture/memory.md)：数据模型、时间、替代、擦除和恢复边界。
- [贡献指南](../CONTRIBUTING.md)：开发环境、测试、构建和调试。
- [评测说明](../eval/README.md)：基线、消融、指标和结果校验。
- [开发约定](../AGENTS.md)、[静态展示页来源](../landing/README.md)。
- [版本变化](../CHANGELOG.md)、[漏洞报告](../SECURITY.md)、[许可](../LICENSE)及[商业授权](../COMMERCIAL-LICENSE.md)。

缺陷、需求和待验证事项在 [GitHub Issues](https://github.com/NotZhu/MnemoTrace/issues) 维护。
