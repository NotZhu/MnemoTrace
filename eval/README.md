# 评测方法与复现

本目录保留通用评测、报告和结果校验工具。当前页面说明如何测量，不维护历史成绩榜单。公开结论的要求见 [AGENTS.md](../AGENTS.md#principle-i)。

## 选择测量范围


| 问题                    | 入口                                                                 |
| ------------------------- | ---------------------------------------------------------------------- |
| 验证 harness 的固定响应回归 | tests/test_eval_harness.py、test_validate_results.py、test_report.py |
| LongMemEval 统一 QA     | [bench.py](bench.py)，数据解析见 [longmemeval.py](longmemeval.py)    |
| LOCOMO 适配             | [locomo.py](locomo.py) 转为同一 harness 的输入                       |
| PersonaMem              | [personamem.py](personamem.py)，日志使用自己的 schema                |
| 功能消融与参数          | bench 的 --ablate 与 [tune_weights.py](tune_weights.py)              |
| 服务接入与导入覆盖      | [facility.py](facility.py)，不是答案准确率基准                       |
| 准备控制台演示数据      | [seed_console.py](seed_console.py)，不是生产用户数据迁移器           |

离线回归、探索切片、完整 QA 和会话收尾验证回答不同问题，不能互相替代。算法切片用于定位变化，影响全局排序、预算或公开结论时扩大数据范围。

窗口抽取可用 `python eval/window_extraction.py --events 24 --runs 3 --out .artifacts/eval/windows.json`
对照即时逐条处理与批量窗口。默认仅回放精确的合成输入/输出，检查调用次数、事实遗漏、来源和模型延迟；
未提供的真实 token 数保持 null。显式指定 `--extractor` 才会使用已配置的真实模型并可能产生费用。
这是抽取专项，不含 answerer/judge，也不代表真实模型质量或端到端收益；公开结论仍须运行下述完整 QA 与 full-context baseline。

### 抽取质量用例与评分

`fixtures/extraction_quality.json` 固定 40 个合成中文场景，八类各五例；24 例为开发集，
16 例为保留集，八个核心场景均在开发集。先冻结用例及预期，再运行候选；
保留集不用于提示词调参。修改错误标注须更新用例版本并重新测量两侧。

`extraction_quality.py` 对模型原始条目和最终存储状态分别评分：仅按预先标注的等价三元组、
身份别名与工作内容匹配，不使用模糊匹配或 LLM judge。一般亲属关系不能代替明确的长幼信息。
来源允许组合、来源时间、历史失效和替代链分别核对；长输入另核对实际引用片段是否覆盖证据。
原始条目不计算未来事实遗漏，最终状态计算整个场景的遗漏；合法的重复抽取不当作无依据新增。
人工事实没有原文来源，身份更新独立于普通 Fact 计分。未标注的有效自由表达须人工复核，不能自动算通过。

报告同时列出事实 precision/recall、匹配条目的来源 precision/recall、事实与来源联合正确率、
具体性损失和工作记忆类型差异。来源分不替代整体召回率，空集分母显示不适用。
固定响应测试验证评分与执行管线，不能证明真实模型质量。

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

### 控制台演示数据

`seed_console.py` 使用基准或合成数据建立演示 namespace，不是生产用户数据迁移器。每个问题的原始序号（从 1 开始）是空间 ID；目录名由核心库的命名空间规则生成，例如空间 `1` 对应 `1--6b86b273ff34fce1`。空间 ID 与登录凭证需要区分。

1. 选择独立的输出目录，明确指定数据范围、embedder 和 LLM。默认配置可能加载模型或调用付费 LLM，先通过 `--help` 核对设置。下面的最小示例全程离线。
2. 检查生成的 `manifest.json` 和 `manifest.md`。JSON 中每条记录的 `namespace` 是空间 ID，`directory` 是输出根目录下的相对存储目录名；其余字段描述问题和记忆数量。使用 `--shard i/N` 时只生成对应的 `manifest.shard<i>.json`，按各分片清单操作，没有自动合并步骤。`--offset` 和 `--limit` 不会重新编号。
3. 停止目标服务，确认清单中的空间 ID 都是本次演示空间。将清单列出的每个完整目录从 `<out>/<directory>` 复制到 `<MNEMOTRACE_DATA_DIR>/<directory>`，保留目录名。目标目录已存在时停止操作，不覆盖或合并；不要把整个 `<out>` 再嵌套到数据根目录下。
4. 配置目标服务使用与生成时相同的 embedder，并启动服务。开放模式下用 `namespace`（例如 `1`）登录；受保护模式下由管理员将 API Key 映射到该空间，使用该 API Key 登录。受保护部署不能只凭空间编号登录。核对记忆内容和清单中的 `facts_live`，重启服务后再次核对。

最小离线示例：将以下合成输入保存为 `.artifacts/console-demo-input.json`，从仓库根目录生成到一个全新的输出目录：

```json
[{"question_type":"synthetic","question":"Where do I live?","answer":"Paris","haystack_sessions":[[{"role":"user","content":"I live in Paris."}]]}]
```

```sh
python eval/seed_console.py --data .artifacts/console-demo-input.json --out .artifacts/console-demo --embedder hashing --llm deepseek
```

按上述步骤复制后，空间 `1` 应能看到居住地 `Paris`。若仅在本机核对复制结果，可先不启动 HTTP 服务，直接使用服务的读取入口（将路径替换为本次复制目标）：

```sh
python -c "from mnemotrace.service import MemoryService; s = MemoryService(data_dir=r'<MNEMOTRACE_DATA_DIR>', embedder_name='hashing', llm_name=''); print(s.memories('1')['facts'])"
```

脚本会在加载模型前检查本次计划写入的空间目录和清单文件；任何目标已存在就报错，并以排他创建防止两个生成进程覆盖同一目标。不同分片可以共享输出根目录，但重复运行同一分片会报错。失败可能留下不完整的目录或清单；换新的 `--out` 重新生成，不把失败输出复制到服务中。旧版编号目录不会自动读取、认领或迁移，旧源存储保持不动；需要数据时重新生成。

所有测量都应在授权范围内执行。核心、安装和工程检查成功不表示算法提升。

所有执行自然语言事实抽取的评测与种子生成入口需要显式 LLM。离线测试使用固定合成响应，只验证管线行为，不能证明模型质量；公开质量结论仍遵循统一评测及完整日志要求。
