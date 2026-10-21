# Benchmark 结果日志

本目录保存原始 JSONL 与工程验收产物。实验持久化、证据消费者与提交范围统一见
[AGENTS.md 的实验产物与日志提交](../AGENTS.md#evidence-artifacts)，发布校验要求见
[评测规则](../AGENTS.md#principle-i)。本页提供工具操作说明。

## 报告与校验

```bash
python eval/report.py results/<name>.jsonl
python eval/validate_results.py --expected-rows <full item count> --require-complete results/<name>.jsonl
```

LongMemEval_S 全集的 `<full item count>` 为 `500`；其他数据集使用真实完整条目数。
多系统日志可用重复的 `--system <name>` 参数选择被引用的系统，例如：

```bash
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean --system full_context results/<name>.jsonl
```

PersonaMem-v2 日志来自 `eval/personamem.py`，并采用一套不同的选择题 schema；请用 `--schema personamem` 与采样条目数来校验它们（例如，对应 `--n-personas 20 --per-persona 5` 使用 `--expected-rows 100`）。

## 产物类型示例

`commercial_release_0_1_0_validation.jsonl` 记录安全、打包、SDK、前端、容器与回归门禁，属于工程发布证据。其证据范围与算法运行的区别见上述日志提交规则。
