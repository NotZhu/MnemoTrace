# MnemoTrace · 忆痕

[![CI](https://github.com/NotZhu/MnemoTrace/actions/workflows/ci.yml/badge.svg)](https://github.com/NotZhu/MnemoTrace/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue.svg)](LICENSE)

为 LLM 智能体提供长期记忆，包含 Python 库、HTTP 服务、MCP 工具、TypeScript SDK 和管理控制台。

MnemoTrace 保存原始会话，从中提取带时间和来源的事实，并跨会话检索相关上下文。知识更新可以保留被替代的事实，支持历史查询。自然语言事实抽取需要配置 LLM；原文保存、手动事实、已有记忆读取和快照恢复可独立使用。默认哈希嵌入仍可用于词汇烟测，不代表语义检索质量。

[English](README.md) · [文档导航](docs/README.md) · [快速开始](docs/quickstart.md)

## 试用

在源码仓库根目录执行，需要 Python 3.10 或更高版本：

```sh
python -m pip install ".[llm]"
# Set MNEMOTRACE_LLM and the provider credentials before running.
mnemotrace-quickstart
```

需要持久化向量后端时，可选安装 `mnemotrace-memory[qdrant]` 或 `mnemotrace-memory[lancedb]`；Qdrant 支持本地与远程模式，配置及生命周期见[配置参考](docs/reference/configuration.md#可选-qdrant)。

HTTP 服务、MCP 工具、SDK 客户端和 CLI 使用 Postgres 记忆，并显式选择空间。Python 直连使用 `PostgresMemory`。直连数据库可安装 `mnemotrace-memory[postgres]`，运行服务可安装 `mnemotrace-memory[serve]`。见 [Postgres 基础配置](docs/reference/configuration.md#postgres-数据库基础)。

以下示例使用内存版 Python 库。

```python
from mnemotrace import Memory
from mnemotrace.llm.providers import make_llm

memory = Memory(llm=make_llm("deepseek"))
memory.remember_messages([
    {"message_id": "m1", "role": "user", "content": "I live in Paris."}
], user_id="user123", session_id="demo")
assert memory.flush_messages(user_id="user123")["ok"]
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
