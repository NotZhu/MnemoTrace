# Benchmark 结果日志

本目录保存 benchmark 声明的原始 JSONL 证据。请将其视为审计日志，而非临时转储。

## 实验持久化规则

每次算法变更的每次实验运行，都必须在 `results/` 下留下一份已保存的产物，或在 PR 中指明已记录的路径。这包括 smoke ablation、固定切片真实数据检查、失败的 provider 尝试，以及完整的 benchmark 运行。

- 优先使用机器可读的 JSONL，通过 `--out`、`--jsonl` 或一小段已保存的 runner 输出。
- 记录确切的命令、数据集或切片、系统、日期、指标与状态。
- 明确标记失败、部分或探索性的产物；绝不要将它们作为可发布的 benchmark 证据呈现。

## 提交规则

只有在有明确消费者时，结果日志才能作为证据提交：

- `README.md` / `README.zh-CN.md` / `RESULTS.md` 公开数字
- 标记为 `DONE` 的论文表格、检查清单或 runbook
- 测试所用的回归 fixture

在提交一个新的被引用的全集运行之前，先校验它：

```bash
python eval/report.py results/<name>.jsonl
python eval/validate_results.py --expected-rows <full item count> --require-complete results/<name>.jsonl
```

校验器必须在日志被标记为 `DONE` 或用于公开文案之前通过。
对于 LongMemEval_S 全集运行，`<full item count>` 为 `500`；对于其他数据集，使用该 benchmark 真实的完整条目数。
当公开文案仅引用多系统日志中选定的系统时，请用可重复的 `--system <name>` 标志显式校验这些系统。这能在不阻塞干净被引用基线的前提下，让已披露的辅助系统失败保持可见。
PersonaMem-v2 日志来自 `eval/personamem.py`，并采用一套不同的选择题 schema；请用 `--schema personamem` 与采样条目数来校验它们（例如，对应 `--n-personas 20 --per-persona 5` 使用 `--expected-rows 100`）。

## 不应作为证据提交的内容

当本地探索性文件属于以下情形时，不要将其引用或提交为 benchmark 证明：

- 部分运行
- smoke 切片
- 失败的竞争对手运行
- prompt/rerank 重建切片
- 未校验的跨 benchmark 实验

这些文件在开发过程中有用，但它们要么应当被完成并校验，要么在进入仓库之前被显式记录为探索性产物。

普通 `.log` 文件被 `.gitignore` 忽略；除非论文产物明确需要，否则请将终端记录保留在本地。

## 发布校验日志

当某份规范或发布报告引用时，工程发布证据也可存储于此。
例如，`commercial_release_0_1_0_validation.jsonl` 记录了 0.1.0 交付物的安全、打包、SDK、前端、容器与回归门禁。这些日志仅用于证明发布就绪状态：它们不是算法实验，绝不能被引用为 benchmark 质量或 accuracy 证据。
