# 证据目录

本目录只保存被公开结果、设计取舍、必要工程发布或回归 fixture 消费的材料。实验本地留存和证据提交的规则见 [AGENTS](../AGENTS.md#evidence-artifacts)。新试跑先写 `.artifacts/<task>/`，不为每次运行新增 Markdown。

| 分类 | 入口与边界 |
| --- | --- |
| 正式 benchmark | [benchmarks](benchmarks/)，公开数字及精确设置由 [RESULTS](../RESULTS.md) 维护 |
| 算法实验 | [实验索引](experiments/README.md)，含消融、context 切片、失败及回滚反证 |
| 工程验收 | [工程索引](engineering/README.md)，与准确率、线上部署核验区分 |

## 历史快照 tag

历史说明统一引用 [`repository-snapshot-2026-10-23`](https://github.com/NotZhu/MnemoTrace/tree/repository-snapshot-2026-10-23)，固定到重构前提交 `c9a51899e63e6c98857b60aaae06471202cb3641`。该 tag 保留原目录、已退役脚本和当时说明；后续整理、amend、rebase 或 squash 不移动或删除它。

快照用于保留文档来源，不代表实验运行代码版本；实验实际 SHA、未提交补丁及未知条件仍以原始记录为准。克隆未包含 tag 时可运行：

```bash
git fetch origin tag repository-snapshot-2026-10-23
git rev-parse 'repository-snapshot-2026-10-23^{commit}'
```

## 报告与校验

```bash
python eval/report.py results/benchmarks/headline_500.jsonl
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean --system full_context results/benchmarks/headline_500.jsonl
```

`LongMemEval_S` 全集为 500 行；其他数据集使用真实完整条目数。混合日志用可重复的 --system 只验证被引用系统，未引用或出错系统不使整份日志成为干净证据。

PersonaMem-v2 使用独立选择题 schema，校验显式加 --schema personamem；例如 20 personas × 5 items 使用 --expected-rows 100。离线消融、context-only 与工程 JSONL 不套用正式 QA schema。

原始文件中的历史命令、路径、系统标识和数值保留不变；当前工具和说明使用分类后的路径。未记录的代码条件或中断原因标为未知，不以目录位置推断完成状态。历史文档快照链接帮助追溯已有命令，不替代运行代码 SHA。

正式证据用 `report/validator` 重算；结果审计默认只扫描 benchmarks 的 QA 日志，实验按 schema 和切片显式选择输入。工程记录与原始脚本不作为当前可执行命令自动运行。
