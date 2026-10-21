# 实施计划：记忆系统参考雷达

**Branch**: `002-memory-reference-radar` | **Date**: 2026-06-29 | **Spec**: [spec.md](spec.md)

**Input**: 功能规格 `specs/002-memory-reference-radar/spec.md`

## 概要

建立内部架构雷达，让 MnemoTrace 研究优秀的公开记忆、GraphRAG、知识编译和智能体学习系统，
仅推进那些能通过独立实现转化为 MnemoTrace 自身能力、并接受可复现评测的模式。
本计划优先处理治理与设计：记录参考来源，将能力模式映射至 MnemoTrace 战略下注，
采纳候选在实施前必须通过独立实现、来源、零配置，以及有评测框架证据支撑的消融门禁。

首批可进入计划阶段的候选：

1. 面向 `supersedes` 演化链的链感知检索。
2. 加强原始证据在片段、事实和图路径之间的融合。
3. 摘要、心智模型、意图 / 程序记录等派生记忆层，以及可选的便于人阅读的工作区视图。
4. 面向多跳扩展的图邻近检索。
5. 用于程序性经验的反思 / 经验记忆。
6. 对准确率 / token 消耗 / 延迟权衡进行测量的运行配置。

## 技术背景（Technical Context）

**Language/Version**: Markdown 文档和 Spec-Kit 产物；后续实现按 MnemoTrace 宪章继续使用 Python >=3.10。

**Primary Dependencies**: 本功能无依赖。外部仓库仅作为研究输入。

**Storage**: 仓库跟踪的 Markdown 文件，位于 `specs/002-memory-reference-radar/`；不改变运行时存储。

**Testing**: 通过检查清单审阅、链接检查、占位符扫描及后续 `/speckit-tasks` 验收检查验证文档。
获准推进的算法变更，后续必须运行 MnemoTrace benchmark 框架，同时报告准确率、token 消耗和 p50/p95 延迟。

**Target Platform**: 在 MnemoTrace 仓库中工作的内部维护者与贡献者。

**Project Type**: 文档 / 治理功能，为后续库与评测框架工作提供输入。

**Performance Goals**: 本文档功能不适用。获准推进的候选必须在实施前定义各自的 benchmark 假设。

**Constraints**: 保持零配置不变量、原始片段 + 事实检索、双时间来源追踪、非破坏性失效处理、独立实现边界，以及诚实的对外措辞。

**Scale/Scope**: 初始雷达覆盖直接记忆系统、图检索系统、知识编译系统、终身记忆系统、反思 / 经验系统、产品接入面和评测来源。本功能不实现运行时行为。

## 宪章检查

*门禁：Phase 0 研究前必须通过；Phase 1 设计后复查。*

- **I. 可复现性不可妥协**：PASS。雷达将外部声明标为仅外部声明（external-only），公开声明前要求 MnemoTrace 自有日志。
- **II. 零配置不变量**：PASS。本功能只增加文档；未来依赖在另行制定规格前明确保持可选。
- **III. 接口优先，后端置于接口之后**：PASS。采纳候选在实施前必须映射至 MnemoTrace 模块，不能直接从雷达引入第三方代码。
- **IV. 不允许静默损坏记忆**：PASS。候选推进需保留来源；涉及事实时，需保留 `supersedes` / 失效处理。
- **V. 先测量，再优化**：PASS。每个获准推进的候选都需要消融假设，并报告准确率 / token 消耗 / 延迟（accuracy/tokens/latency）。
- **VI. 组合，而非单选**：PASS。雷达明确组合原始片段、事实、时间图谱、分层结构、反思和运行配置，不单选某个外部模式。
- **VII. 诚实、开放的对外措辞**：PASS。措辞约束是一等需求；竞争对手声明在 MnemoTrace 复现前仅作内部材料。

**设计后复查：** PASS。`data-model.md`、`contracts/` 和 `quickstart.md` 保留相同门禁，增加验证步骤，不提供绕过实施约束的捷径。

## 项目结构

### 本功能的文档

```text
specs/002-memory-reference-radar/
├── plan.md
├── spec.md
├── research.md
├── technical-report.zh-CN.md
├── data-model.md
├── quickstart.md
├── checklists/
│   └── requirements.md
└── contracts/
    ├── adoption-candidate.md
    └── radar-entry.md
```

### 源代码（仓库根目录）

```text
AGENTS.md                 # 项目章程与工作规则的唯一维护来源
CLAUDE.md                 # Claude Code 导入入口，仅导入 AGENTS.md
.specify/
├── feature.json          # 当前功能指针
└── memory/
    └── constitution.md   # 本计划使用的治理门禁
specs/
└── 002-memory-reference-radar/
    └── ...               # 本功能的规划产物
```

**结构决策：** 本功能仅涉及文档。不在 `mnemotrace/` 增加代码路径，不在 `tests/` 增加测试，
不在 `results/` 增加 benchmark 日志。后续实施功能从采纳候选中拆出，单独规划。

## 复杂度跟踪

无需宪章违规豁免或额外复杂度例外。
