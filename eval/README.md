# 评测方法与复现

本目录保留通用评测、报告和结果校验工具。当前页面说明如何测量，不维护历史成绩榜单。公开结论的要求见 [AGENTS.md](../AGENTS.md#principle-i)。

## 选择测量范围


| 问题                    | 入口                                                                 |
| ------------------------- | ---------------------------------------------------------------------- |
| 验证 harness 和离线行为 | tests/test_eval_harness.py、test_validate_results.py、test_report.py |
| LongMemEval 统一 QA     | [bench.py](bench.py)，数据解析见 [longmemeval.py](longmemeval.py)    |
| LOCOMO 适配             | [locomo.py](locomo.py) 转为同一 harness 的输入                       |
| PersonaMem              | [personamem.py](personamem.py)，日志使用自己的 schema                |
| 功能消融与参数          | bench 的 --ablate 与 [tune_weights.py](tune_weights.py)              |
| 服务接入与导入覆盖      | [facility.py](facility.py)，不是答案准确率基准                       |
| 准备控制台演示数据      | [seed_console.py](seed_console.py)，不是生产用户数据迁移器           |

离线回归、探索切片、完整 QA 和会话收尾验证回答不同问题，不能互相替代。算法切片用于定位变化，影响全局排序、预算或公开结论时扩大数据范围。

## 准备运行

源码安装的 extras 以 [pyproject.toml](../pyproject.toml) 为准。真实 QA 通常需要 llm、所选 embedder 的依赖，以及对应 provider 凭据；完整 CLI 参数用 `python eval/bench.py --help` 查看。

bench 的 --data 可传本地 JSON 路径，也支持 s/m/oracle 等 LongMemEval 数据别名和 locomo 适配入口。别名可能需要额外下载依赖与联网；本地路径更便于固定数据版本。遵循数据集原始来源的获取和使用条件，记录版本、题目 id 范围及预处理。

以下为 Bash 运行模板：先设置 EXTRACTOR_MODEL、ANSWERER_MODEL、JUDGE_MODEL、EMBEDDER_MODEL 为 factory 支持且已配置的模型标识，再执行。此路径会调用模型并可能产生费用：

```sh
mkdir -p .artifacts/eval/my-run
python eval/bench.py \
  --data /path/to/longmemeval_s.json \
  --limit 20 --systems engram_lean,full_context \
  --extractor "$EXTRACTOR_MODEL" --answerer "$ANSWERER_MODEL" \
  --judge "$JUDGE_MODEL" --embedder "$EMBEDDER_MODEL" \
  --pin-extraction .artifacts/eval/my-run/extraction-cache \
  --out .artifacts/eval/my-run/qa.jsonl
```

engram_lean 是当前 CLI 的系统标识，并非另一份产品名。使用脚本实际支持的标识，不因文案重写更名。

## 数据、基线与消融

- --limit 0 表示不限制题数；--full 是开启算法功能的选项，不是“运行全集”。
- --qid、--category、--failures-from 控制切片，选样发生在 --limit 等处理之前；记录实际 QID，不能只报告请求数量。--shuffle 改变所选样本顺序，不能自动消除切片偏差。
- 记忆系统与 full_context 在同次运行中共享明确的 answerer/judge 和数据条件；声明上下文预算、摄入、模型和配置差异。
- 比较读路径时复用 --pin-extraction，避免抽取差异掩盖读取变化；这不能消除 answerer/judge 的随机性。
- --ablate 关闭对应配置开关，具体映射见 bench.py。多次运行的代码、模型、预算和范围应一致，原始开关状态应记录。
- --resume 只用于恢复相同系统和配置的同一次运行。更换模型或系统后不要复用旧输出混合恢复。
- --emit 为官方 judge 输出适配日志；它与 bench 自身 QA JSONL 的 schema 不同。

当前 harness 不通过 close_session 验证会话结论生命周期；该路径使用专门的服务/持久化回归和 Agent 示例核对。

## 报告与完整性

```sh
python eval/report.py .artifacts/eval/my-run/qa.jsonl
python eval/validate_results.py --expected-rows 20 --require-complete .artifacts/eval/my-run/qa.jsonl
python eval/audit_results.py .artifacts/eval/my-run/qa.jsonl --bench-rows 20 --fail-invalid
```

完整 LongMemEval_S 对应 500 题，不能用上例 20 行探索结果替代。混合日志可用重复的 --system 只要求所引用系统完整，但应明确系统选择，不能把未被检查的部分也称为完整。PersonaMem 使用 `--schema personamem` 并按实际实验设计指定题数。

report 输出和日志字段需要分别理解：

- accuracy 的有效分母、错误与未完成题数；
- total tokens 与单次上下文估算，是否包含抽取、回答、判断等调用；
- p50/p95 延迟及其测量范围，区分摄入、检索和端到端；
- full-context 对照、配置差异和多次运行波动。

审计工具必须显式传入日志路径或 glob；批量审计时为 glob 加引号，由工具统一展开。不传路径会报告用法错误，不扫描隐含目录，也不猜测哪些日志可作为证据。

## 产物与公开证据

本地实验输出和抽取缓存放在 .artifacts/ 的任务目录，保留精确命令、数据范围、系统、模型、参数、代码 commit、未提交 diff 和结果。失败及未完成尝试明确标注，必要复现实验不能只留最终成功摘要。

公开数字需要可持续获取的原始日志与复现条件，发布前通过完整性校验并确认所有入口表述一致。可按实际发布决定持久保存位置；临时 CI 日志和本地被忽略文件本身不足以提供长期可获取性。普通测试不新增验收 Markdown 或历史结果归档目录。

## 保留工具的边界

facility 会读取所选会话目录、watcher/hook 状态并可能访问服务，使用前核对 --roots、--url、凭据和输出范围；--no-out 可禁止追加记录。它的覆盖统计不能替代用户体验或答案质量。

seed_console 使用基准数据建立演示 namespace，输出为 JSONL 存储目录。显式选择 --out 目录、数据范围、embedder 和 LLM，避免覆盖真实用户数据；先通过 --help 核对默认设置。

所有测量都应在授权范围内执行。核心、安装和工程检查成功不表示算法提升。
