# MnemoTrace · 忆痕

[English](README.md) | [中文](README.zh-CN.md)

面向 LLM 智能体的开源长期记忆引擎。忆痕保存原始会话，抽取双时间事实，保留被替代的历史，并将事实与来源片段共同检索。

**0.1.0 Beta · 单节点自托管。** 离线闭环不需要 API key 或服务；生产 HTTP 必须配置凭据。入口见[部署](deploy/README.md)、[安全策略](SECURITY.md)与[文档](docs/README.md)。

## 快速开始

使用 Python 3.10 或更新版本，在当前检出运行：

```bash
python examples/quickstart.py
```

在项目中导入包时安装本地源码：

```bash
python -m pip install -e .
mnemotrace-quickstart
```

发行包名为 `mnemotrace-memory`；以上命令不代表已有 PyPI/npm 发布。核心仅用标准库，以确定性哈希嵌入、规则抽取和内存存储完成闭环。

```python
from mnemotrace import Memory

memory = Memory()
memory.add("My name is Alex and I work at Acme.", user_id="user123")
memory.consolidate()
print(memory.search("Where does Alex work?", user_id="user123").answer())
```

跨会话共享使用相同的原始 `user_id`，不同编号保持隔离；姓名和昵称识别仅在同用户范围内工作。详见[快速开始](docs/quickstart.md)、[Python API](docs/reference/python-api.md)和[后端实现状态](docs/architecture/README.md#backend-status)。

## 验证结果

LongMemEval_S 全集 500 题，使用官方分类 judge 提示。该次记录采用 doubao-seed-2.0-pro 作答、DeepSeek 官方接口判分；比较系统使用相同作答器、judge 与题目。

| 系统 | 准确率 | 平均上下文 token |
| --- | ---: | ---: |
| MnemoTrace（`engram_lean`） | **84.4%** | **7.9k** |
| Full-context 基线 | 78.8% | 79.2k |

该次运行高出 **5.6 个百分点**，上下文 token 约为基线的 **1/10**。这些是历史实测，不是后续代码的实时属性。更早的 83.6% / 73.2% 属于另一 judge 设置，单独保留记录。

[RESULTS.md](RESULTS.md)说明精确设置、总 token 与延迟报告限制、类别结果、供应商变化及复现命令。[原始日志](results/benchmarks/run500_v3_main_deepseekjudge.jsonl)已提交，可用 `python eval/report.py <file>` 重算；[评测工具](eval/README.md)提供 full-context 对照和完整性校验。

## 接入 Agent 或应用

- [Agent 指南](docs/guides/agents.md)：本地 MCP、HTTP 代理、安装、自检与会话收尾。
- [HTTP API](docs/reference/http-api.md)：受保护服务、控制台、分页和 OpenAI-compatible memory 扩展。
- [MCP 工具](docs/reference/mcp.md)与 [TypeScript SDK](clients/typescript/README.md)。
- [部署说明](deploy/README.md)：Docker Compose、systemd、备份、密钥轮换和升级边界。

可选服务能力通过 `python -m pip install -e '.[serve]'` 安装。启动已配置的服务后访问 `/ui/`。当前可用后端与规划中集成明确区分。

## 设计与贡献

[架构](docs/architecture/README.md)说明写入、整合、检索和服务边界；[设计取舍](docs/decisions.md)关联采纳及反证；[问题台账](docs/issues.md)维护未完成状态。

贡献前阅读 [CONTRIBUTING.md](CONTRIBUTING.md)和 [AGENTS.md](AGENTS.md)。普通改动更新对应源码与测试，重大变更使用按风险选择的规格流程。

[论文](https://arxiv.org/abs/2606.09900) · [论文源码](paper/README.md) · [演示](https://notzhu.github.io/MnemoTrace/)

## 许可

采用 AGPL-3.0，条款见 [LICENSE](LICENSE)。不遵守 AGPL 的专有或 SaaS 用途须取得[商业授权](COMMERCIAL-LICENSE.md)。
