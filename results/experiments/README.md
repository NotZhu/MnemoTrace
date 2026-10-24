# 算法实验与测量限制

本页保存采纳、否决和回滚所需的历史证据入口。以下均为探索或局部门禁；公开全集成绩只见 [RESULTS](../../RESULTS.md)。原始 JSONL 不重写，格式与当前校验见[证据说明](../README.md)。

每节的“历史说明”固定到[历史快照 tag](../README.md#历史快照-tag)，包含原有命令、切片与已知代码条件；该文档快照的 commit 不是实验运行代码 SHA。没有记录的条件标为未知，不补造复现信息。旧命令使用当时布局，当前分析按下方文件链接运行；新试跑输出到 .artifacts/。

<a id="aggregation-constraint-filter"></a>

## 聚合约束过滤

以题面月份等局部约束标记数值候选；离线消融及相关 context 切片只验证候选接线与筛选，不证明完整 QA 提升。

原始产物：[aggregation_constraint_filter_ablation.jsonl](aggregation-constraint-filter/aggregation_constraint_filter_ablation.jsonl)、[aggregation_constraint_filter_lme_s_context27.jsonl](aggregation-constraint-filter/aggregation_constraint_filter_lme_s_context27.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/aggregation_constraint_filter_experiments.md)。

<a id="aggregation-recall-expansion"></a>

## 聚合召回扩展

为 count/list/sum 组织高召回子查询。候选覆盖、上下文成本与最终答案质量需要分别测量。

原始产物：[aggregation_recall_expansion_ablation.jsonl](aggregation-recall-expansion/aggregation_recall_expansion_ablation.jsonl)、[aggregation_recall_expansion_lme_s_context30.jsonl](aggregation-recall-expansion/aggregation_recall_expansion_lme_s_context30.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/aggregation_recall_expansion_experiments.md)。

<a id="chain-provenance-promotion"></a>

## 演化链来源提升

用 supersedes 链中旧事实的来源补原始细节，复用 chain_evidence 开关；目标消融与上下文样本不替代全集质量。

原始产物：[chain_provenance_promotion_ablation.jsonl](chain-provenance-promotion/chain_provenance_promotion_ablation.jsonl)、[chain_provenance_promotion_context_sample.jsonl](chain-provenance-promotion/chain_provenance_promotion_context_sample.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/chain_provenance_promotion_experiments.md)。

<a id="numeric-aggregation"></a>

## 数值聚合候选

从事实、会话和摘要形成带来源的数值表，供 answerer 使用；候选命中不等同于算术与约束都正确。

原始产物：[numeric_aggregation_candidates_ablation.jsonl](numeric-aggregation/numeric_aggregation_candidates_ablation.jsonl)、[numeric_aggregation_lme_s_context30.jsonl](numeric-aggregation/numeric_aggregation_lme_s_context30.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/numeric_aggregation_candidates_experiments.md)。

<a id="preference-extraction"></a>

## 显式偏好抽取

补齐 prefer/avoid/love/enjoy 类表达。证据包含离线消融和 preference 类别 context 切片，不是公开准确率增益。

原始产物：[preference_extraction_ablation.jsonl](preference-extraction/preference_extraction_ablation.jsonl)、[preference_extraction_lme_s_context30.jsonl](preference-extraction/preference_extraction_lme_s_context30.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preference_extraction_experiments.md)。

<a id="preference-object-filter"></a>

## 弱偏好对象过滤

具体对象保留，it/these ideas 等弱对象过滤。历史 30 条 preference 切片报告弱对象 45→0、结构化偏好覆盖仍为 7/30；这是抽取精度检查，不是 QA 得分。

原始产物：[preference_object_filter_ablation.jsonl](preference-object-filter/preference_object_filter_ablation.jsonl)、[preference_object_filter_lme_s_context30.jsonl](preference-object-filter/preference_object_filter_lme_s_context30.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preference_object_filter_experiments.md)。

<a id="preference-object-normalization"></a>

## 偏好对象规范化

移除 sound/idea/concept of 等包装。历史 30 条切片报告包装对象 114→0、结构化偏好覆盖仍为 7/30；不能外推为全集收益。

原始产物：[preference_object_normalization_ablation.jsonl](preference-object-normalization/preference_object_normalization_ablation.jsonl)、[preference_object_normalization_lme_s_context30.jsonl](preference-object-normalization/preference_object_normalization_lme_s_context30.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preference_object_normalization_experiments.md)。

<a id="preference-reversal"></a>

## 偏好反转抽取

高置信 no longer/stopped enjoying 表达触发非破坏性替代。历史 LongMemEval_S 模式扫描 500 条无命中，LOCOMO 扫描记录当时数据缺失；不能将扫描称为 500 题 QA。

原始产物：

- [preference_reversal_extraction_ablation.jsonl](preference-reversal/preference_reversal_extraction_ablation.jsonl)
- [preference_reversal_lme_s_pattern_scan.jsonl](preference-reversal/preference_reversal_lme_s_pattern_scan.jsonl)
- [preference_reversal_locomo_pattern_scan.jsonl](preference-reversal/preference_reversal_locomo_pattern_scan.jsonl)

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preference_reversal_extraction_experiments.md)。

<a id="procedural-extraction"></a>

## 规程抽取

仅明确 runbook/procedure/workflow/checklist 或 To/How to 动作形式进入规程层；早期把普通角色对话当作流程的规则被否决。长步骤不投影为实体。

原始产物：[procedural_extraction_ablation.jsonl](procedural-extraction/procedural_extraction_ablation.jsonl)、[procedural_extraction_context25.jsonl](procedural-extraction/procedural_extraction_context25.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/procedural_extraction_experiments.md)。

<a id="procedural-memory"></a>

## 规程读取

有来源的 standing rule/runbook 单独呈现。stride-50 运行没有从该切片抽出规程块，提示覆盖问题，不能用中性运行声称 QA 提升。

原始产物：[procedural_memory_ablation.jsonl](procedural-memory/procedural_memory_ablation.jsonl)、[procedural_memory_context50.jsonl](procedural-memory/procedural_memory_context50.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/procedural_memory_experiments.md)。

<a id="provenance-chunk-promotion"></a>

## 事实来源原文提升

命中事实后补原始会话，但保留语义 chunk 名额。早期 stride-50 对照的来源覆盖未改善；provider QA 失败尝试单独保留，不作为完整质量证据。

原始产物：

- [provenance_chunk_promotion_ablation.jsonl](provenance-chunk-promotion/provenance_chunk_promotion_ablation.jsonl)
- [provenance_chunk_promotion_context50.jsonl](provenance-chunk-promotion/provenance_chunk_promotion_context50.jsonl)
- [provenance_chunk_promotion_qa_attempt_failed.jsonl](provenance-chunk-promotion/provenance_chunk_promotion_qa_attempt_failed.jsonl)
- [provenance_chunk_promotion_recall_sample.jsonl](provenance-chunk-promotion/provenance_chunk_promotion_recall_sample.jsonl)

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/provenance_chunk_promotion_experiments.md)。

<a id="summary-fallback"></a>

## 来源摘要回退

fact-miss how-to 的目标消融证明回退接线，stride-50 的来源覆盖没有改善。来源摘要补充原文，不能替代完整来源或据此声称全集收益。

原始产物：

- [summary_fallback_ablation.jsonl](summary-fallback/summary_fallback_ablation.jsonl)
- [summary_fallback_context50.jsonl](summary-fallback/summary_fallback_context50.jsonl)
- [summary_fallback_recall_sample.jsonl](summary-fallback/summary_fallback_recall_sample.jsonl)

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/summary_fallback_experiments.md)。

<a id="readpath"></a>

## 读路径测量与合包归因

早期前 60 题 answer-in-context 基线重复为 70.0%/78.3%，噪声约 8.3pp。来源提升曾挤占语义 chunk，后改为最多占部分预算。100 题合包改善不能分别归因，纯 multi-session 60 题未复现 aggregation_pool_boost 的独立收益；该开关当前仍开启。更强 answerer 的探索结果不足以证明 benchmark 上限或 90% 不可达。

原始产物：

- [longmemeval_s_engram_full_pro.jsonl](readpath/longmemeval_s_engram_full_pro.jsonl)
- [longmemeval_s_engram_lean_baseline.jsonl](readpath/longmemeval_s_engram_lean_baseline.jsonl)
- [longmemeval_s_engram_lean_v1.jsonl](readpath/longmemeval_s_engram_lean_v1.jsonl)
- [readpath_ablation.jsonl](readpath/readpath_ablation.jsonl)
- [readpath_postfix_baseline.jsonl](readpath/readpath_postfix_baseline.jsonl)
- [run100_p1p0b_deepseekjudge.jsonl](readpath/run100_p1p0b_deepseekjudge.jsonl)
- [run500_final_main_deepseekjudge.jsonl](readpath/run500_final_main_deepseekjudge.jsonl)
- [run500_semanticfloor_deepseekjudge.jsonl](readpath/run500_semanticfloor_deepseekjudge.jsonl)
- [run50_postfix_deepseekjudge.jsonl](readpath/run50_postfix_deepseekjudge.jsonl)
- [run50_prefix_deepseekjudge.jsonl](readpath/run50_prefix_deepseekjudge.jsonl)
- [run50_round2_deepseekjudge.jsonl](readpath/run50_round2_deepseekjudge.jsonl)
- [run60_gpt56sol_answerer_probe.jsonl](readpath/run60_gpt56sol_answerer_probe.jsonl)
- [run60_multisession_p0b_deepseekjudge.jsonl](readpath/run60_multisession_p0b_deepseekjudge.jsonl)

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/docs/readpath-improvement-plan.zh-CN.md)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/readpath_ablation_report.md)。

<a id="temporal"></a>

## 时间粒度与回滚

历史测量先后改变归因，最终说明为 T1 在目标类别未证实收益、已回滚，T2 改动抽取 prompt 导致两臂事实集不同，不能用同一固定抽取试验确认。缓存和按时间串行身份注册降低了上下文波动，但 answerer/judge 仍有方差。部分最终试验只有历史说明，缺少相应完整原始日志，不作为正式 QA 证据。

原始产物：[locomo200_main_deepseekjudge.jsonl](temporal/locomo200_main_deepseekjudge.jsonl)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/docs/temporal-granularity-plan.zh-CN.md)。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/locomo_2026-08-29.md)。

<a id="entity-normalization"></a>

## 实体归一化回滚

按中文设定的长度、词数与标点阈值误伤英文实体，任一端不合格即丢边造成图邻近丢失。历史回滚对照只支持撤销该实现，不否定一般实体归一化。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/entity_normalization_regression_ab.md)。

<a id="embedding"></a>

## 嵌入与中文适用性

历史个人环境诊断指出 hashing/英文模型与中文语料不匹配的风险；这是特定设置的诊断，不是所有中文任务或后端的性能保证。

[历史说明与原命令](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/embedder_zh_2026-09-01.md)。
