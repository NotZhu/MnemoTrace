# 数据模型：记忆系统参考雷达

**Feature**: `002-memory-reference-radar`

本模型定义雷达中可供评审的记录。当前以 Markdown 表格和章节表示，以下字段是未来工具和任务应保留的契约。

## 实体：参考来源（Reference Source）

与 MnemoTrace 长期记忆战略相关的公开仓库、产品页、论文、benchmark 或技术合集。

**字段：**

- `name`：稳定的来源名称。
- `link`：规范的公开 URL。
- `category`：类别，可选直接记忆系统（direct memory system）、时间图谱记忆（temporal graph memory）、GraphRAG、知识编译（knowledge compilation）、终身记忆（lifelong memory）、反思 / 经验记忆（reflection/experience memory）、产品接入面（product surface）、benchmark 或雷达合集（radar collection）。
- `priority`：P0、P1、P2 或 benchmark/radar。
- `architecture_signal`：观察到的最有价值设计模式。
- `mnemotrace_learning`：MnemoTrace 可在架构层面学习什么。
- `adoption_candidate`：该来源支持的 MnemoTrace 自有候选，或 `research-only`。
- `evidence_status`：仅外部声明（External-only）、已本地复现（reproduced locally）、已在 MnemoTrace 评测框架验证（benchmarked in MnemoTrace harness），或被消融实验否定（rejected by ablation）。
- `clean_room_license_status`：仅研究架构（Architecture-only）、许可待审（license pending）、许可已审（license reviewed），或已阻止（blocked）。
- `last_verified`：最近核验链接和主要声明的日期。

**校验规则：**

- 每个参考来源必须包含公开链接。
- 每个参考来源必须明确标注证据状态。
- 来源采用 copyleft 许可，或许可信息不明确 / 缺失时，必须标为仅研究架构，直到后续计划审查许可影响。
- 外部 benchmark 声明只能作为来源自身的声明进行概述，不能复制到 MnemoTrace 公开结果中。

## 实体：能力模式（Capability Pattern）

从一个或多个参考来源中观察到的可复用架构思想。

**字段：**

- `name`：简短的模式标签。
- `source_refs`：一个或多个参考来源。
- `mnemotrace_native_form`：该模式如何映射到 MnemoTrace 原语。
- `strategic_bets`：一项或多项 MnemoTrace 战略下注 A-F。
- `expected_benefit`：用户可见或 benchmark 可见的改善。
- `risk`：已知风险，例如提示词膨胀、依赖过重、来源丢失或 benchmark 过拟合。

**校验规则：**

- 模式必须映射到 MnemoTrace 原语，不能只写外部项目名称。
- 模式必须保留原始证据 + 整合记忆的组合，否则应明确拒绝或重新设计。
- 本功能中的模式不得要求新增强制依赖。

## 实体：采纳候选（Adoption Candidate）

已获准推进、可成为后续 Spec-Kit 功能或任务组的能力模式。

**字段：**

- `name`：候选名称。
- `status`：仅研究（Research-only）、可进入计划（planning-ready）、已计划（planned）、已实现（implemented）、已评测（benchmarked）、已拒绝（rejected）或已归档（archived）。
- `source_patterns`：启发此候选的能力模式。
- `affected_surface`：受影响部分，可选写入路径（write path）、整合（consolidation）、图存储（graph store）、读取路径（read path）、画像记忆（profile memory）、程序性记忆（procedural memory）、运行配置（runtime profile）、工作区导出（workspace export）或评测框架（evaluation harness）。
- `scope`：验证候选所需的最小变更。
- `ablation_plan`：如何对比启用和禁用候选时的效果。
- `benchmark_target`：测试预期行为的 benchmark 或合成数据子集。
- `metrics`：准确率、token 消耗和 p50/p95 延迟的预期变化方向。
- `rollback_criterion`：出现哪些证据时不应交付该候选。
- `messaging_impact`：无影响（None）、仅内部（internal-only），或经 MnemoTrace 自主复现后公开（public after MnemoTrace-owned reproduction）。

**校验规则：**

- 没有 benchmark 目标和消融计划，候选不得进入 planning-ready 状态。
- 涉及事实的候选必须保留来源、双时间戳和非破坏性失效处理。
- 增加外部依赖的候选必须保持零配置行为不变。

## 实体：证据记录（Evidence Record）

某项声明或候选的证明状态。

**字段：**

- `claim`：待评估的声明。
- `source`：外部来源、MnemoTrace 运行，或本地检查。
- `status`：仅外部声明（External-only）、已本地复现（reproduced locally）、已在 MnemoTrace 评测框架验证（benchmarked in MnemoTrace harness）、已被反驳（contradicted），或证据不足（insufficient evidence）。
- `artifact`：可用时，提供证明材料链接或路径。
- `metrics`：若已评测，记录准确率、token 消耗、延迟和错误数量。
- `notes`：范围限制或相对 benchmark 默认设置的偏离。

**校验规则：**

- 公开性能声明需要 MnemoTrace 自有原始日志。
- 用于公开声明的 benchmark 证据必须包含同一次运行的全上下文基线。
- 仅外部声明的证据不得用作 MnemoTrace 性能证明。

## 实体：措辞约束（Messaging Guardrail）

区分内部研究与公开声明的规则。

**字段：**

- `rule`：约束正文。
- `applies_to`：适用范围，可为 README、文档、落地页、benchmark 报告、发布说明或内部计划。
- `reason`：约束背后的宪章原则或项目风险。
- `check`：审阅者如何核验。

**校验规则：**

- 公开文案不得点名竞争对手作 benchmark 比较，除非 MnemoTrace 已在自身评测框架中复现该比较，且文案包含完整三项指标。
- 没有已提交的 MnemoTrace 证据时，公开文案不得宣称“SOTA”“#1”“世界最佳”等。
- 内部文档可列出参考名称，但必须将外部声明标为仅外部声明。

## 状态转换

```text
参考来源（Reference Source）
  discovered（已发现）-> reviewed（已审阅）-> periodically reverified（定期复核）
                      -> archived（过时或不相关时归档）

能力模式（Capability Pattern）
  observed（已观察）-> mapped-to-MnemoTrace（已映射）-> candidate-proposed（已提出候选）
                   -> rejected（违反不变量或缺乏可测量假设时拒绝）

采纳候选（Adoption Candidate）
  research-only -> planning-ready -> planned -> implemented -> benchmarked
                -> rejected/archived（消融失败或风险超过收益时拒绝 / 归档）

证据记录（Evidence Record）
  external-only -> reproduced locally -> benchmarked in MnemoTrace harness
                -> contradicted 或 insufficient-evidence（检查失败时被反驳或标为证据不足）
```
