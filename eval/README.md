# MnemoTrace 评测

本目录是 MnemoTrace 基准声明的可复现承载面。核心规则很简单：只有在原始 JSONL 日志被提交、可报告且经验证时，一个数字才可对外发布。

## 离线冒烟评测框架

先运行零密钥项目冒烟测试：

```bash
python scripts/check_zero_setup.py
```

该命令会执行快速上手、合成评测框架、已提交公开日志的验证、论文统计，以及无需可选依赖的标准库编译。若只想运行合成评测框架：

```bash
python eval/harness.py
python eval/harness.py --storage durable --out results/synthetic_durable.jsonl
python eval/report.py results/synthetic_durable.jsonl
```

合成日志用于回归和评测框架形态检查。它们不是公开的基准证据。

## 算法消融冒烟

当新增读路径特性时，在投入 LongMemEval/LOCOMO 之前，先在零密钥合成消融中证明它们添加了预期的证据：

```bash
python eval/ablate_features.py
```

该命令会逐个切换最新的证据特性，并检查启用路径是否呈现了禁用路径无法呈现的证据：supersedes 链上下文、针对历史值问题的链感知溯源分块提升、来自 supersession 历史的自然语言历史值答案、针对 fact-miss how-to 查询的派生摘要回退、针对 runbook/规则查询的溯源支撑过程记忆、从 runbook/how-to 文本中基于规则的过程抽取、溯源支撑的原始片段、溯源引导的完整明细分块提升、n-hop 图邻近、在紧凑预算下保留精确原始明细的意图感知证据预算分配，以及将目标关系排在同节点干扰项之前的查询条件化图关系加权，外加将多路径支撑目标排在单路径干扰项之前的 PPR 风格图路径增强，以及面向第一人称图查询的自锚定。它是目标行为的局部改进证明，而非公开的准确率声明。它还检查唯一短名实体锚定，因此只有当该 token 无歧义时，"Moonshot" 才能安全锚定 `Moonshot AI`；并检查负向图约束，因此 "not in Lisbon" 会过滤被排除的路径而非提升它；外加 planner 位置/项目链，使同事/公司、人/项目问题能从 `works_at` 或 `works_on` 延续到 `based_in`/`located_in` 答案事实。

可发布的声明仍需 `eval/bench.py` 加已提交的原始日志。

## 算法 PR 门禁

每个算法读/写路径 PR 都必须同时包含本地正确性检查和真实数据验收：

```bash
python eval/ablate_features.py --jsonl results/<feature>_ablation.jsonl
pytest -q tests/test_lean.py tests/test_smoke.py tests/test_zero_setup_default.py tests/test_eval_ablation.py
python eval/validate_results.py --expected-rows 500 --require-complete --system engram_lean results/headline_500.jsonl
```

对于预计会影响基准行为的改动，需在 LongMemEval、LOCOMO 或 PersonaMem 上新增一次 `eval/bench.py` 运行（PR 门禁允许使用固定切片；公开声明仍需完整的已提交日志）。PR 描述必须报告命令、行数、被比较的系统、准确率、token、p50/p95 延迟、保存的制品路径，以及结果是回归、中性还是改进。失败或中断的实验也必须作为 failed/partial 制品保存，而非默默丢弃。

## 真实基准

产品及运行时包名已改为 MnemoTrace / `mnemotrace`。为复现已有 raw logs，benchmark system IDs
（`SYSTEMS` keys、`system.name`、日志 `sys` keys）仍保留 `engram`、`engram_lean` 等历史标识；
这些是评测身份，不是旧包、旧启动命令或旧环境变量的运行时兼容入口。证据文件路径也保持原样。

`eval/bench.py` 是 LongMemEval/LOCOMO/PersonaMem 风格运行的统一评测装置。同一次运行中的每个系统共享相同的回答器、评分器、抽取器和数据切片，因此运行内比较是唯一可公平对照的声明。

真实运行写出 `results/<name>.jsonl` 后，同时使用以下两个工具：

```bash
python eval/report.py results/<name>.jsonl
python eval/validate_results.py --expected-rows <full item count> --require-complete results/<name>.jsonl
```

- `report.py` 汇总所写入的一切：准确率、上下文 token、p50/p95 延迟、错误，以及已评分的分母。
- `validate_results.py` 在日志不足以引用时失败：行数错误、重复的 qid、缺失字段、未评分行或错误行。
  对于 LongMemEval_S 全量运行，`<full item count>` 为 `500`；其他数据集请使用该基准的真实全量条目数。
  若多系统日志包含一个探索性的或被显式标记为错误的、不属于已发布声明的系统，请显式验证被引用的系统：

```bash
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean --system full_context results/<name>.jsonl
```

- `audit_results.py` 扫描杂乱的本地 `results/` 目录，并将每个日志标记为 `complete`、`incomplete` 或 `invalid`，以免探索性日志被误当作证据。
- `compare.py` 针对单个系统，在其共享的已评分 qid 上比较两条或多条已验证日志，报告每条日志的准确率、分歧，以及多骨干互补性的 oracle 上限：

```bash
python eval/compare.py results/backbone_a.jsonl results/backbone_b.jsonl --system engram_lean --per-category
```

只有通过验证的日志才应在论文笔记中标记为 `DONE`，或用于 README/RESULTS 文案。部分日志、冒烟切片、失败的竞品运行和探索性消融可保留在本地，直到完成或被明确记为探索性。原始日志的提交策略见 [`../results/README.md`](../results/README.md)。

在决定提交什么之前，先审计该目录：

```bash
python eval/audit_results.py
python eval/audit_results.py --fail-invalid results/headline_500.jsonl results/bb_flash.jsonl
```

PersonaMem-v2 是一个多选题运行器，采用不同的 JSONL 形态（`pref_type` + 选中的选项，而非 LongMemEval 的 `cat` + 经评分的 `pred/gold`）。请使用显式 schema 验证它：

```bash
python eval/personamem.py --n-personas 20 --per-persona 5 --out results/personamem_v2.jsonl
python eval/report.py results/personamem_v2.jsonl
python eval/validate_results.py --schema personamem --expected-rows 100 --require-complete results/personamem_v2.jsonl
```

## 后续实验

当前的多骨干、竞品、LOCOMO 和方差运行见 [`M2_RUNBOOK.md`](M2_RUNBOOK.md) 与 [`../paper/EXPERIMENTS_CHECKLIST.md`](../paper/EXPERIMENTS_CHECKLIST.md)。
