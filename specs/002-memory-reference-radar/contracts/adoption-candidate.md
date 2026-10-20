# 契约：采纳候选

本契约定义参考模式从研究进入后续 Spec-Kit 功能、任务组或 benchmark 实验的条件。

## 必填字段

| 字段 | 是否必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 候选名称 |
| `source_patterns` | 是 | 提供启发的模式和来源 |
| `mnemotrace_native_form` | 是 | 所改变的 MnemoTrace 原语或能力接口 |
| `strategic_bets` | 是 | 候选服务的战略下注 A-F |
| `affected_surface` | 是 | 写入路径、整合、图存储、读取路径、画像记忆、程序性记忆、工作区导出、运行配置或评测框架 |
| `minimum_scope` | 是 | 可评测的最小有用版本 |
| `benchmark_target` | 是 | 测试该行为的 benchmark、类别或合成数据子集 |
| `metrics` | 是 | 准确率、token 消耗和 p50/p95 延迟的预期变化方向 |
| `ablation_plan` | 是 | 如何对比启用和禁用候选时的效果 |
| `rollback_criterion` | 是 | 出现哪些证据时不应交付候选 |
| `clean_room_notes` | 是 | 许可和独立实现边界 |
| `messaging_impact` | 是 | 无影响、仅内部，或经 MnemoTrace 自主复现后公开 |

## 推进规则

- 进入 `/speckit-tasks` 前，`benchmark_target`、`metrics` 和 `ablation_plan` 必须具体明确。
- 涉及事实的候选必须保留来源、双时间戳和 `supersedes` 链。
- 涉及读取路径的候选必须保留原始证据 + 整合记忆，除非 MnemoTrace 自主进行的消融实验证明其他路径更好。
- 需要可选服务、模型或存储的候选，必须保持零配置 quickstart 和测试可用。
- 在 MnemoTrace 原始日志证明结果之前，对外措辞影响仍限于内部。

## 示例

```text
name: 链感知检索
source_patterns: Hy-Memory 演化链；Graphiti 时间更新；MnemoTrace supersedes 模型
mnemotrace_native_form: 检索当前事实及长度受限、带来源的 supersedes 链
strategic_bets: Bet B, Bet C, Bet D
affected_surface: 读取路径、时间过滤、上下文组装
minimum_scope: 在 token 预算内，将检索事实扩展到一跳的已被替代 / 替代它的事实
benchmark_target: LongMemEval/LOCOMO 中的知识更新与时间推理子集，以及合成更新测试
metrics: 准确率提升，token 消耗不变或略增，p95 延迟保持在读取路径目标内
ablation_plan: 对相同题目和回答模型，对比基线检索与链感知检索
rollback_criterion: 准确率收益未超出波动范围，提示词膨胀损害延迟 / token 消耗，或来源存在歧义
clean_room_notes: 基于 MnemoTrace 现有事实模型实现，不复制外部代码
messaging_impact: 在 MnemoTrace 评测框架验证前仅限内部
```
