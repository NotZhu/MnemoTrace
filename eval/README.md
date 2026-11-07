# 评测方法与复现

本目录保留通用评测、报告和结果校验工具。当前页面说明如何测量，不维护历史成绩榜单。公开结论的要求见 [AGENTS.md](../AGENTS.md#principle-i)。

存储改造的固定响应基线使用 `python -m eval.storage_baseline --out <全新输出目录>`，复用本目录合成评测；[契约与复现说明](../docs/architecture/storage-contracts.md#行为对照与验证输入)包含稳定观察及数据库负载对照。它不调用真实模型，不代表数据库性能或真实模型质量。

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
用例第2版只修正 append-2 的重复事实时间：分别抽取后消解重复可保留首次有效时间，
同窗合并引用时可采用末次被引用的 pending 时间；两者均允许，其他事件时间仍严格检查。
该修正不增加关系、实体、来源或工作内容的等价表达。第1版仍可读取以复现已保存证据。

`extraction_quality.py` 对模型原始条目和最终存储状态分别评分：仅按预先标注的等价三元组、
身份别名与工作内容匹配，不使用模糊匹配或 LLM judge。一般亲属关系不能代替明确的长幼信息。
来源允许组合、来源时间、历史失效和替代链分别核对；长输入另核对实际引用片段是否覆盖证据。
原始条目不计算未来事实遗漏，最终状态计算整个场景的遗漏；合法的重复抽取不当作无依据新增。
人工事实没有原文来源，身份更新独立于普通 Fact 计分。未标注的有效自由表达须人工复核，不能自动算通过。

报告同时列出事实 precision/recall、匹配条目的来源 precision/recall、事实与来源联合正确率、
具体性损失和工作记忆类型差异。来源分不替代整体召回率，空集分母显示不适用。
固定响应测试验证评分与执行管线，不能证明真实模型质量。

真实服务对照显式选择模型并指定新目录，例如：

```sh
python eval/window_extraction.py --quality --extractor deepseek \
  --baseline-ref 85a4ebc --candidate-ref <candidate-commit> \
  --out .artifacts/extraction-quality/my-run \
  --budget-file .artifacts/extraction-quality/shared-budget.json
python eval/window_extraction.py --report .artifacts/extraction-quality/my-run
```

两个版本均从指定提交导出，在独立子进程中使用同一 runner。存储为独立内存快照，嵌入器为 hashing，
冲突模型和会话结论关闭；两组均完成摘要并保留工作记忆供评分。这是同等收尾对照，不是默认完整服务或问答基准。
按抽取、摘要及未知阶段分别报告调用、提供方 token 和延迟；写入确认与场景完成耗时分别统计。
缓冲的 confirmed_to_window_start 从本窗最早来源的写入确认计至模型开始，包含调度及前窗处理等待；
模型在确认前已启动时记为0，不将其当作纯缓冲停留时间。
原始条目只报告已输出内容的精确性，召回与遗漏在最终状态计算，避免重复抽取产生大于 1 的原始召回率。

先运行核心八例的两版本、两模式；候选核心未通过即停止扩量，完整40例及核心额外两轮明确标记 not_run。
通过后补齐完整集合，再补核心两轮。核心须三轮均通过；完整集合的事实、来源和联合指标不得下降，
且不得新增错误主体、有效时间或历史链等严重错误。自由概括的未匹配结果保持待复核，不自动通过。
`--report` 从原始观察重新评分并检查重复、缺失、用例哈希及终态，不发起模型调用；退出 0 表示运行时验收通过，
退出 1 包括质量未通过或执行不完整。保留失败、预算终止和全部原始响应，不仅保留成功结果。

预算默认累计2000次模型请求、300万已上报token、60分钟，可通过 `--max-calls/--max-tokens/--max-seconds`
在新账本创建时设置。多次候选使用同一个 `--budget-file`，已有账本不会重置；请求失败仍计次数，
缺失 usage 时停止后续请求。达到阈值后停止发起新请求，在途响应可能越过 token 停止阈值，因此不是精确费用硬上限。
DeepSeek 在创建存储或占用请求预算前检查凭据是否已配置，凭据值不进入日志。
内置模型客户端单次尝试、45秒超时，后台任务重试仍计入记录和预算。

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

产品入口的会话整理与操作进度由服务集成回归和 Agent 示例核对。

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

## 连续会话缓冲对照

`window_extraction.py --buffered-replay` 在同一个固定运行时上比较 `immediate` 和 `buffered`。
场景集为十二个合成中文会话（六类，每类 8/24 条消息），默认两轮共 48 条观测。
默认运行时为 `3089ce1`，保留原提示词和 16384 字节输入预算；模型调用数不能代替 token 或整个服务成本。默认引用必须是 main 上可达的提交：分支经 rebase 合并后旧 SHA 会消失。

生产者按固定时间表投递，每个用户的调用按 FIFO 消费；即时模型调用阻塞时，后续消息仍进入客户端队列。
这代表顺序消费的客户端，不是 HTTP 并发吞吐测试。中途刷新使用 `timeout=0` 捕获并强制已有内容，
不等待其完成；最终刷新等待完成，再用相同选项关闭会话、补齐逐事件摘要，保留工作记忆，关闭会话结论。
来源时间在摄入时单独设置，性能测量使用单调时钟；缓冲调度仍使用真实时间。

下面的小集全程使用固定响应，不需要凭据；仍保留实际消息间隔。测试中的可控时间与线程屏障用于快速回归。
输出目录必须不存在，原始证据和预算在该目录内保存。

```sh
python eval/window_extraction.py --buffered-replay --cases burst-short --runs 1 --out .artifacts/replay-offline
```

显式指定真实模型才会访问付费服务。父进程预先加载本地配置并检查凭据，配置文件与凭据不复制到产物。
默认请求温度 0、客户端单次尝试、45 秒超时；服务后台仍保留原来的瞬时失败重试，各次尝试全部计量。
默认预算是 2000 次请求、300 万已报告 token、14400 秒，任一上限或缺失用量会阻止后续请求。
在途用量仍计入，token 阈值不是精确费用封顶。缺失用量不按零处理，旧预算不得重置以继续同一批次。

```sh
python eval/window_extraction.py --buffered-replay --extractor deepseek-chat --pilot-only --out .artifacts/replay-real
python eval/window_extraction.py --buffered-replay --extractor deepseek-chat --resume --out .artifacts/replay-real
python eval/window_extraction.py --report .artifacts/replay-real
```

预检为短档连续消息、持续追加和在途追加场景，双模式共六条观测，计入完整矩阵。
观测不完整、来源错误、隔离错误或计量缺失时停止扩展。恢复只补从未开始的观测，不重跑失败结果；
有启动日志而无终态记录的观测视为归属不明，停止恢复。原运行时、模型、场景、轮数与工具哈希必须保持一致。
每个场景有 900 秒子进程上限，退出或超时保留已记录请求，预算中未结算的请求阻止继续付费。

报告按模式、轮次和场景分列抽取/摘要/总调用及 token，包含所有失败尝试。
提供方调用失败与抽取协议拒绝分别统计；HTTP 成功或完整用量不代表模型输出被系统接受。
成本节省只从双方都完成且用量完整的配对总量计算，同时列出被排除配对。
时序区分计划到达、生成、进入调用、原文保存、返回及抽取范围完成；超过 250ms 的生产者偏差样本
保留并单列，不用于稳定延迟结论。大事件的完成时间取最后一个必要片段的持久化时间。
窗口记录输入字节组成、利用率、来源范围、同时满足的触发条件；重启读取核对最终事实、原文、工作记忆及处理状态。
离线模式只证明管线与合窗机制，不提供真实 token 或模型质量结论；本评测也不证明摘要质量、QA 准确率或生产吞吐。

所有执行自然语言事实抽取的评测与种子生成入口需要显式 LLM。离线测试使用固定合成响应，只验证管线行为，不能证明模型质量；公开质量结论仍遵循统一评测及完整日志要求。
