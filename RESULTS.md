# 基准结果与复现边界

本页是公开数字的维护入口。表格由已提交 JSONL 重算；它们属于各自的历史运行，不代表当前分支重新完成了全集评测。规则见 [AGENTS](AGENTS.md#principle-i)，工具见[评测操作](eval/README.md)。论文及其历史实验材料保留在 [paper](paper/)。

## 公开主记录：2026-08-28，LongMemEval_S

[原始日志](results/benchmarks/run500_v3_main_deepseekjudge.jsonl)包含同题、同 answerer/judge 的两个系统，均完成 500/500，0 错误。证据提交为 `693d5b21a2083a285d37d7a856f46d79d808316a`；提交日志的版本不等于已经恢复了运行时全部未提交状态。

| 系统 | 准确率 | 平均上下文 token | 回答延迟 p50 / p95 |
| --- | ---: | ---: | ---: |
| MnemoTrace，`engram_lean` | 84.4% | 7,909（7.9k） | 52.9s / 117.7s |
| `full_context` | 78.8% | 79,241（79.2k） | 14.6s / 58.4s |

准确率高出 **5.6 个百分点**，平均上下文约为基线的 **1/10**（约 10× 差距）；lean 的回答延迟更高。日志中的 tok 是上下文 token，不能据此推算总 token 或 API 账单；lat 是回答路径耗时，不能作为纯检索延迟。完整摄入、抽取、总 token 与成本未由这份 schema 充分记录。

| 类别 | MnemoTrace | full-context | 题数 |
| --- | ---: | ---: | ---: |
| single-session-assistant | 94.6% | 94.6% | 56 |
| single-session-user | 90.6% | 81.2% | 64 |
| knowledge-update | 84.7% | 90.3% | 72 |
| temporal-reasoning | 82.7% | 80.3% | 127 |
| single-session-preference | 80.0% | 50.0% | 30 |
| multi-session | 77.7% | 65.3% | 121 |
| abstention | 90.0% | 93.3% | 30 |

此表与 report.py 一致，并纳入拒答题；旧正文中部分分类百分比不对应这份日志，已校正。历史说明记载本轮使用 DeepSeek 官方 judge；完整精确启动命令、依赖锁定及运行时 diff 未在该 JSONL 中保存，因此这里只保证重算日志，不声称可逐字恢复这次运行。复跑需重新记录完整条件，并使用同一次运行的基线。

```bash
python eval/report.py results/benchmarks/run500_v3_main_deepseekjudge.jsonl
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean --system full_context results/benchmarks/run500_v3_main_deepseekjudge.jsonl
```

## 历史记录：已退役 judge 的 500 题运行

下列结果保留独立设置，不与上一轮做跨 judge 的直接增益比较。历史文档将该轮关联到 `4a2aa0e`；其模型、命令及已知 endpoint 限制见下文。

| 系统 | 准确率 | 平均上下文 token | 已评分 / 总题数 |
| --- | ---: | ---: | ---: |
| `engram_lean` | 83.6% | 9,568（9.6k） | 500/500 |
| `full_context` | 73.2% | 79,241 | 500/500 |
| `engram_full`（完整历史加事实） | 83.4% | 79,541 | 499/500，1 错误 |

lean 的回答延迟为 p50 **60.5s** / p95 **106.6s**。它与 full-context 的准确率差为 +10.4 个百分点，平均上下文约少 8 倍；该比较只说明这轮 rig 的观察结果。

| lean 类别 | 准确率 | 题数 |
| --- | ---: | ---: |
| single-session-assistant | 92.9% | 56 |
| single-session-user | 87.5% | 64 |
| knowledge-update | 87.5% | 72 |
| abstention | 86.7% | 30 |
| temporal-reasoning | 81.1% | 127 |
| multi-session | 79.3% | 121 |
| single-session-preference | 73.3% | 30 |

原始证据：

- [lean 日志](results/benchmarks/longmemeval_s_engram_lean_v2_final.jsonl)，500 行。
- [full-context 与 full 变体](results/benchmarks/longmemeval_s_volcano_doubao_deepseekjudge.jsonl)，其中只有 full_context 适合作为完整无错误证据。
- [论文主日志](results/benchmarks/headline_500.jsonl)与[第二骨干日志](results/benchmarks/bb_flash.jsonl)，由 [compute_stats.py](paper/compute_stats.py)分别汇总。不同 rig 保持独立，不把其数值混入本表。

### 历史运行条件与命令

历史说明记载：LongMemEval_S 全集，`BAAI/bge-small-en-v1.5`，抽取器 `doubao-seed-1.6-flash`，回答器 `doubao-seed-2.0-pro`，judge `deepseek-v3.2`，使用官方类别专属 judge 提示。`engram_lean` 仅读取检索到的事实、原文片段与摘要，`full_context` 使用完整历史。

以下保留原有复现命令。历史记录在 2026-08 报告该 Volcano judge endpoint 已关闭；它不作为现在可成功执行的服务承诺。模型调用前需核对提供方及版本，换 judge 后生成独立结果，不能替换历史分数：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python eval/bench.py \
    --data s --limit 500 --systems engram_lean,full_context \
    --answerer volcano:doubao-seed-2-0-pro-260215 \
    --judge volcano:deepseek-v3-2-251201 \
    --extractor volcano:doubao-seed-1-6-flash-250615 \
    --embedder bge-small --reasoning --persona \
    --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
    --out data/run.jsonl
```

重算已提交结果不需要调用模型：

```bash
python eval/report.py results/benchmarks/longmemeval_s_engram_lean_v2_final.jsonl results/benchmarks/longmemeval_s_volcano_doubao_deepseekjudge.jsonl
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean results/benchmarks/longmemeval_s_engram_lean_v2_final.jsonl
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system full_context results/benchmarks/longmemeval_s_volcano_doubao_deepseekjudge.jsonl
python paper/compute_stats.py --bootstrap-samples 100
```

`--system` 可重复使用；仅当日志中每个系统都作为完整证据时省略。`engram_full` 的一条错误不能因只报告已评分分母而消失。

## 测量限制与未证实结论

历史质量修正包括 full-context 截断、通用 judge 与官方提示的差异，以及 `_abs` 拒答处理。旧的截断基线不可用；`engram_full` 包含完整历史，不能单独证明检索效果。相同 rig 只控制实验条件，不保证 lean 必然胜出，也不消除模型随机性。

≥ +8 的历史优势目标未达成；三轮 500 题的记录为 +5.8、+4.0、+5.6。更强 answerer、时间措辞和读路径候选的正反证保存在[实验索引](results/experiments/README.md#readpath)。小样本或换 answerer 的探索不能证明检索已无改进空间；其他论文的 oracle 会话成绩也不是本 rig 的理论上限。当前没有据此成立的任意规模、恒定成本或全配置质量保证。

## 设施测量

[facility.py](eval/facility.py)独立衡量记忆是否存在、会话是否关闭、是否被调用及可观察的替代链。它不使用 LLM，不输出内容、查询或会话标识；“recall 是否改变答案”和“关闭一周是否有损害”仍需人工研究，不能由调用计数代替。

```bash
python eval/facility.py --days 7 --url http://127.0.0.1:8766 --key-file ~/.mnemotrace/watch.key
```

默认本地趋势文件为 `.artifacts/eval/facility.jsonl`；历史工程样本见[工程证据](results/engineering/README.md#facility)。`--assert-no-regress` 区分回归（1）与测量失败（2）；个人机器上的设施数字不作为 benchmark 成绩公开。
