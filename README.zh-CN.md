# MnemoTrace · 忆痕

为 LLM 智能体提供长期记忆，包含 Python 库、HTTP 服务、MCP 工具、TypeScript SDK 和管理控制台。

MnemoTrace 保存原始会话，从中提取带时间和来源的事实，并跨会话检索相关上下文。知识更新可以保留被替代的事实，支持历史查询。Python 默认路径使用哈希嵌入与规则抽取，无需 API key、模型下载或外部服务；这套确定性配置用于理解管线，不代表语义检索质量。

[English](README.md) · [文档导航](docs/README.md) · [快速开始](docs/quickstart.md)

## 试用

在源码仓库根目录执行，需要 Python 3.10 或更高版本：

```sh
python -m pip install .
mnemotrace-quickstart
```

最小接入示例：

```python
from mnemotrace import Memory

memory = Memory()
memory.add("I live in Paris.", user_id="user123", session_id="demo")
memory.consolidate()
print(memory.search("Where do I live?", user_id="user123").answer())
# Paris
```

该实例只在内存中存活。保存和重开见[快速开始](docs/quickstart.md)；运行持久化服务和控制台见[部署说明](deploy/README.md)。

## 选择入口


| 目标                     | 阅读入口                                                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 使用与管理记忆           | [使用指南](docs/guides/usage.md)                                                                                                                     |
| 接入 Agent 或 MCP 客户端 | [Agent 接入](docs/guides/agents.md)                                                                                                                  |
| 调用接口                 | [Python](docs/reference/python-api.md)、[HTTP](docs/reference/http-api.md)、[MCP](docs/reference/mcp.md)、[TypeScript](clients/typescript/README.md) |
| 配置模型与存储           | [配置参考](docs/reference/configuration.md)                                                                                                          |
| 理解实现                 | [架构总览](docs/architecture/README.md)                                                                                                              |
| 开发与测量变更           | [贡献指南](CONTRIBUTING.md)、[评测说明](eval/README.md)                                                                                              |

模型抽取、真实嵌入与作答需要相应的可选依赖和配置。控制台需要构建前端，单独安装 Python wheel 不包含这份构建。

## 许可与安全

许可见 [LICENSE](LICENSE) 与[商业授权条款](COMMERCIAL-LICENSE.md)。漏洞报告方式见[安全政策](SECURITY.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。
