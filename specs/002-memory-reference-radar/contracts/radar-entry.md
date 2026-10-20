# 契约：雷达条目

`research.md` 中的每个来源都应能按本契约表示。Markdown 表格可将字段分散在不同章节，
但任何获准推进的来源都不得遗漏这些字段。

## 必填字段

| 字段 | 是否必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 雷达中统一使用的稳定来源名称 |
| `url` | 是 | 规范的公开 URL |
| `category` | 是 | 来源类别，例如记忆系统、GraphRAG、知识工作区、benchmark 或雷达 |
| `priority` | 是 | P0、P1、P2、benchmark/radar 或 archived |
| `architecture_signal` | 是 | 最值得研究的设计模式 |
| `mnemotrace_learning` | 是 | MnemoTrace 在不复制实现的前提下可以学习什么 |
| `adoption_candidate` | 是 | 此来源支持的候选，或 `research-only` |
| `evidence_status` | 是 | 仅外部声明、已本地复现、已在 MnemoTrace 评测框架验证、已被反驳或证据不足 |
| `clean_room_license_status` | 是 | 仅研究架构、许可待审、许可已审或已阻止 |
| `last_verified` | 建议填写 | URL 与主要内容概述的核验日期 |

## 验收检查

- URL 返回成功响应，或已标为失效并记录检查日期。
- 条目明确声明是仅外部声明，还是已由 MnemoTrace 复现。
- 条目不将 benchmark 数字复制为 MnemoTrace 证据。
- 条目至少能映射到一个能力模式，或明确标为仅研究。
- 条目的许可状态在审查完成前阻止代码复用。

## 示例

```text
name: LLM Wiki
url: https://github.com/nashsu/llm_wiki/blob/main/README_CN.md
category: 知识编译工作区
priority: P1
architecture_signal: 不可变来源 -> 生成的 Wiki -> schema/用途；图谱诊断
mnemotrace_learning: 便于人阅读、有来源支撑的记忆工作区与图谱健康检查
adoption_candidate: 记忆工作区诊断
evidence_status: External-only
clean_room_license_status: GPLv3；仅研究架构，采用 clean-room 独立实现
last_verified: 2026-06-29
```
