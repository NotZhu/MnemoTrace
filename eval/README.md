# 评测操作

项目规则见 [AGENTS](../AGENTS.md#principle-i)，已发布结果和历史条件见 [RESULTS](../RESULTS.md)，日志分类与 schema 见[证据目录](../results/README.md)。本页只维护可执行入口，不保存阶段待办或逐次运行成绩。

## 离线检查

```bash
python scripts/check_zero_setup.py
python eval/harness.py
python eval/harness.py --storage durable --out .artifacts/eval/synthetic_durable.jsonl
python eval/report.py .artifacts/eval/synthetic_durable.jsonl
python eval/ablate_features.py --jsonl .artifacts/eval/feature_ablation.jsonl
```

zero-setup 检查仓库文档、源码与已安装模块 quickstart、CLI help、合成评测、已发布证据、论文统计和标准库编译。合成与离线消融验证目标行为，不作为真实 QA 准确率。读路径消融工具 `ablate_readpath.py` 的默认输出为 `.artifacts/eval/readpath_ablation.jsonl`。

## 真实基准

`bench.py` 提供统一摄入、上下文、回答和评分流水线。系统名保留 `engram_lean`、`full_context` 等历史评测标识；产品包仍为 `mnemotrace`。数据加载、系统和可关闭开关以 [bench.py](bench.py) 为准。

真实运行需要数据、可选模型依赖及相应 provider 的凭据，会产生 API 费用。先使用 `python eval/bench.py --help` 核对参数，为 answerer、judge、extractor 选择明确的模型版本；历史 endpoint 状态见 RESULTS，文档不保证它们继续服务。以下示意命令中的模型占位符需替换：

```bash
python eval/bench.py --data s --limit 500 --systems engram_lean,full_context \
    --answerer <provider:model> --judge <provider:model> --extractor <provider:model> \
    --embedder bge-small --reasoning --persona \
    --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
    --out .artifacts/eval/lme_s.jsonl
python eval/report.py .artifacts/eval/lme_s.jsonl
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean --system full_context .artifacts/eval/lme_s.jsonl
```

LOCOMO 使用 `--data locomo` 并准备对应加载器需要的数据。`--limit 500` 只是上限，不能推定其他数据集已完成全集。更换骨干需完整记录 answerer/judge/extractor、依赖、数据范围、开关、命令与代码状态；跨 rig 的数字分别报告。未验证的第三方适配器不能因注册在 SYSTEMS 就视为可用。

## 校验、审计与比较

- `report.py` 汇总准确率、已评分分母、平均上下文 token、p50/p95 延迟及错误；上下文 token 不等于总 token，端到端延迟不等于检索延迟。
- `validate_results.py` 检查行数、qid、字段和完整评分。LongMemEval_S 全集为 500；混合日志通过可重复 `--system` 只声明被引用系统。
- `audit_results.py` 默认仅扫描 `results/benchmarks/*.jsonl`，将 QA 日志区分 complete/incomplete/invalid。实验与工程 JSONL 需要各自 schema，不能混扫后当作损坏的正式 QA。
- `compare.py` 在共享的已评分 qid 上比较同一系统，报告分歧与组合 oracle；oracle 是这两份输出的事后选择上界，不是 benchmark 的理论上限。

```bash
python eval/audit_results.py
python eval/audit_results.py --fail-invalid results/benchmarks/headline_500.jsonl results/benchmarks/bb_flash.jsonl
python eval/compare.py .artifacts/eval/backbone_a.jsonl .artifacts/eval/backbone_b.jsonl --system engram_lean --per-category
```

PersonaMem-v2 是独立多选题 schema，不能使用 LongMemEval 的 cat/pred/gold 约定：

```bash
python eval/personamem.py --n-personas 20 --per-persona 5 --out .artifacts/eval/personamem_v2.jsonl
python eval/report.py .artifacts/eval/personamem_v2.jsonl
python eval/validate_results.py --schema personamem --expected-rows 100 --require-complete .artifacts/eval/personamem_v2.jsonl
```

新运行先留在 `.artifacts/`；只有明确消费者需要的证据才按[分类规则](../results/README.md)入库。设施测量 `facility.py` 默认写 `.artifacts/eval/facility.jsonl`，独立于 benchmark 准确率；合成测试不读取个人语料。研究中的未完成事项统一在[问题台账](../docs/issues.md)，论文历史实验范围保留在 [paper 检查清单](../paper/EXPERIMENTS_CHECKLIST.md)。
