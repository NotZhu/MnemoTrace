# 忆痕 — 基准测试结果与方法论

本文件是 README 中那个头条数字背后完整、可复现的记录。项目的核心准则（CLAUDE.md，Bet D）：**无法复现的数字不存在。** 重新运行并验证所需的全部内容都在此处。

**论文：** [arXiv:2606.09900](https://arxiv.org/abs/2606.09900) —— 下方头条结果的可同行评审版本。

---

## 头条结果：LongMemEval_S，500 题

头条系统为 **`engram_lean`** —— 它只从一小块*检索*到的记忆切片中作答，而非完整历史。这正是记忆系统的诚实测试：一个精准过滤后的上下文，以极少的 token 击败噪声满满的整窗。

| System | Overall | Avg context tokens | Errors |
|---|---:|---:|---:|
| **MnemoTrace** (`engram_lean`) | **83.6%** | **9.6k** | 0 / 500 |
| full-context baseline (same answerer + judge) | 73.2% | 79k | 0 / 500 |

> [!NOTE]
> **这些数字与生成它们的代码严格绑定** —— 提交 `4a2aa0e`（2026-06-27），即提交 `results/headline_500.jsonl` 的修订版本。此后代码库已大幅演进（图检索、证据规划、时间链、偏好抽取、LLM 查询分解），并**未**在完整 500 题上重新评分；对相同题目的 50 题抽查，整体分数与本基线一致，但各类别分布差异很大（时间推理与偏好上升，单会话检索下降）。请将 83.6 / 73.2 视为*该*修订版本的实测结果，而非 `main` 分支的实时属性。待当前读路径消融确定哪些特性默认开启后，计划针对稳定评分器重新跑一次完整 500 题。

**MnemoTrace `engram_lean` 在以约 8× 更少的 token 使用时，比全上下文基线高出 +10.4 个百分点**（9.6k 对 79k）于本次 500 题运行。两个数字都基于**官方** LongMemEval 评分器提示词，评测框架对每一个系统都施加了**相同的回答器与评分器**；`engram_lean` 与 `full_context` 均完成了全部 500 题，错误数为 0。

> 作为参考，在同一 500 题运行中，一个非 lean 变体将冲突已解决的事实*置于完整历史之上*（`engram_full`，约 79k token）得分为 **83.4%** —— 即 9.6k token 的 lean 检索与"完整历史加事实"变体在约 1/8 的上下文下表现相当。`engram_full` 有 1 条错误项，因此其 83.4% 是基于 499 条已评分题目；我们头条采用的是 lean 数字。

---

## 当前代码测量（2026-08-28，deepseek 官方评分器）

当前 `main` 分支，完整 500 题，**0 错误**，日志已提交并通过格式校验（`results/run500_v3_main_deepseekjudge.jsonl`）：

| System | Overall | Avg context tokens |
|---|---:|---:|
| **MnemoTrace** (`engram_lean`) | **84.4%** | **7.9k** |
| full-context baseline (same answerer + judge) | 78.8% | 79.2k |

**以约 10× 更少的 token 高出 +5.6 个百分点**，在 6 个类别中赢下 4 个：

| Category | MnemoTrace | full-context |
|---|---:|---:|
| single-session-assistant | 94.6% | 94.6% |
| single-session-user | **91.4%** | 82.9% |
| knowledge-update | 84.6% | **91.0%** |
| temporal-reasoning | **83.5%** | 81.2% |
| single-session-preference | **80.0%** | 50.0% |
| multi-session | **78.2%** | 66.9% |

**关于绝对数字的背景：** LongMemEval 作者报告的 *oracle* GPT-4o 为 **~82.4%** —— 即直接交给回答器的黄金会话，也就是完美检索所能带来的上限。MnemoTrace 的 84.4% 来自一块*检索*到的 7.9k 切片，因此过滤后的上下文表现略好于直接把正确会话交给模型。请将其视为一个合理性锚点，而非逐一对应的比较：读者不同、评分器不同。

**我们未能跨越的部分。** 一个预先登记的目标 —— 达到 **≥ +8** 的优势 —— 在三次独立的 500 题运行中均未达成（+5.8、+4.0、+5.6）。三项独立探查表明，剩余的差距不在检索层：(1) 在多会话上，`engram_lean` 与 `full_context` 完全打平，因此答案已不再依赖于提供哪段上下文；(2) 换用更强的推理回答器（`gpt-5.6-sol`）在同一上下文下*丢失*了 1.6 个百分点，同时延迟翻倍；(3) 我们已经处于已发布的 oracle 上限。在此基准上报告 94%+ 的系统并未公开其骨干模型、评分器或题目子集，因此我们不将其视为可比较对象。我们选择如实报告这一未达标之处，而非改写它。

**比分数更值得注意的成本注记：** 在推理回答器下，全上下文基线变得过于昂贵而无法跑完（100 题预计超过 10 小时，已中止），而 7.9k 的 lean 路径在 40 分钟内完成了 60 题。随着读者模型变重，10× 的 token 差距从一项成本节省变成了可行性差异。

## 逐类别明细（`engram_lean`，完整 500 题）

| Category | Score | n | Notes |
|---|---:|---:|---|
| single-session-assistant | 92.9% | 56 | near-ceiling |
| single-session-user | 87.5% | 64 | profile/identity recall |
| knowledge-update | 87.5% | 72 | bi-temporal "most-recent-wins" working |
| abstention | 86.7% | 30 | official *unanswerable* judge |
| temporal-reasoning | 81.1% | 127 | date-stamped context + reasoning chain |
| multi-session | 79.3% | 121 | counting/aggregation across sessions — active area |
| single-session-preference | 73.3% | 30 | hard industry-wide (frontier LLMs 37–48% on PersonaMem) |
| **Overall** | **83.6%** | **500** | |

**效率：** 在已提交的运行中，平均检索到的上下文为 **~9.6k token**（比约 79k 的全上下文基线精简约 8×）。`engram_lean` 的端到端回答延迟在本日志中为 p50 **60.5s** / p95 **106.6s**；这包含了远程的 doubao-seed-2.0-pro 回答调用，并按实测上报，而非用作单独的检索延迟宣称。

---

## 精确设置

- **数据集：** `longmemeval_s`（真实基准，500 题，每题约 50 个会话 / 约 115k-token 的 haystack），取自 HuggingFace `xiaowu0162/longmemeval`。
- **嵌入器：** `BAAI/bge-small-en-v1.5`（本地，无需 API key）。
- **内部抽取器**（System-2 事实抽取）：`doubao-seed-1.6-flash`。
- **回答器：** `doubao-seed-2.0-pro`。
- **评分器：** `deepseek-v3.2`，使用**精确官方 LongMemEval 类别专属评分器提示词**（`eval/longmemeval.py` 中的 `official_judge_prompt()`）发起 —— 时间偏差一位容差、知识更新旧信息容差、偏好评分标准宽松、"包含答案"语义、不可回答检测。
- **`engram_lean` 系统：** MnemoTrace 检索一小块混合切片 —— 冲突已解决的双时间轴事实 + 最相关的原始会话分块 + L2 会话摘要 —— 并仅基于*该*切片作答（约 9.6k token），从不使用完整历史。

### 复现

> [!IMPORTANT]
> **头条运行所用的评分器 endpoint 已被其提供方下线。** 截至 2026-08-27，Volcano Ark 将 `deepseek-v3-2-251201` —— 以及该平台上所有其他 `deepseek-*` 模型 —— 报告为 `status: Shutdown`，并将 `doubao-seed-1-6-flash-250615` 报告为 `status: Retiring`。下方命令**逐字保留，作为已发布数字的溯源**；它正是生成这些数字所用的命令，而如今它将返回 404。如需现在重新运行，请见下方的 *Running it today*。

```bash
# the exact command that produced the headline numbers (judge endpoint since retired -- see above)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python eval/bench.py \
    --data s --limit 500 \
    --systems engram_lean,full_context \
    --answerer volcano:doubao-seed-2-0-pro-260215 \
    --judge volcano:deepseek-v3-2-251201 \
    --extractor volcano:doubao-seed-1-6-flash-250615 \
    --embedder bge-small --reasoning --persona \
    --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
    --out data/run.jsonl
```

```bash
python eval/report.py data/run.jsonl     # prints the per-category table above
python eval/validate_results.py --expected-rows 500 --require-complete \
    --system engram_lean --system full_context data/run.jsonl  # 500 = full LongMemEval_S
```


#### 现在如何运行

换用一个仍在服务的评分器（`deepseek` 即 DeepSeek 官方 API；`univibe:gpt-5.5` 也可用）。其余部分不变：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python eval/bench.py \
    --data s --limit 500 \
    --systems engram_lean,full_context \
    --answerer volcano:doubao-seed-2-0-pro-260215 \
    --judge deepseek \
    --extractor volcano:doubao-seed-1-6-flash-250615 \
    --embedder bge-small --reasoning --persona \
    --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
    --out data/run.jsonl
```

**诚实地看待该结果。** 更换评分器就更换了衡量标尺，因此该命令得出的绝对分数**无法**与上文发布的 83.6 / 73.2 直接比较 —— 那些数字属于测量它们时所用的评分器，我们不会在另一个评分器下重新陈述它们。仍然有效的是你*本次运行内部*的比较：评测框架对你的每一个 `--systems` 条目施加相同的评分器，因此 `engram_lean` 对 `full_context` 仍是逐项可比的。这一内部对比 —— 一小块检索到的切片以极少的 token 击败完整历史 —— 才是真正的论点，也是你今天能够复现的部分。当我们重新发布绝对数字时，将基于一个稳定的评分器，作为一个带有自身已提交日志的新运行，而不是对这些数字静默地重新基线化。评测框架对每一个 `--systems` 条目施加**相同的回答器与评分器**，因此任意*同一运行内部*的比较（例如 `engram_lean` 对 `full_context`）在构造上就是逐项可比的。逐题的原始日志（每题的预测 + 标准答案 + 正确性 + token + 延迟）会写入 `--out` 指定的 JSONL。

**原始日志已提交：**
- `engram_lean` 头条（83.6%，9.6k token）：[`results/longmemeval_s_engram_lean_v2_final.jsonl`](results/longmemeval_s_engram_lean_v2_final.jsonl) —— 500 行。
- 全上下文基线（73.2%，500/500 已评分）+ `engram_full` 变体（83.4%，499/500 已评分，1 处错误），同一 500 题运行，相同回答器 + 评分器：[`results/longmemeval_s_volcano_doubao_deepseekjudge.jsonl`](results/longmemeval_s_volcano_doubao_deepseekjudge.jsonl)。

你可以自己重算任意表格：`python eval/report.py <file.jsonl>`。在将某份日志用作已发布证据之前，请用 `python eval/validate_results.py --expected-rows <完整条目数> --require-complete --system <name> <file.jsonl>` 校验所引用的系统。仅当日志中每个系统都意在作为完整、可引用的证据时，才省略 `--system`。

### 提供方设置

模型以 `provider:model` 的形式指定，并由 `mnemotrace/llm/providers.py` 通过 LiteLLM 解析，因此任何 OpenAI 兼容的 endpoint 都能工作。换上 OpenAI / DeepSeek / 本地模型，相同的命令即可运行。嵌入器与离线回退无需 key。

**Endpoint 漂移 —— 上方命令中的评分器已下线（2026-08-17 核实）。** Volcano/ARK 已撤回 `deepseek-v3-2-251201`；该 id 现在返回 `InvalidEndpointOrModel.NotFound`，因此按原样打印的复现命令在*每个*系统上都尚未评分任何一题便以 `litellm.NotFoundError` 失败。ARK 上已不再提供任何 DeepSeek 模型。回答器（`doubao-seed-2-0-pro-260215`）与抽取器（`doubao-seed-1-6-flash-250615`）不受影响。

如需今天重新运行，请将评分器指向 DeepSeek 自己的 API —— 在 `.env` 中设置 `DEEPSEEK_API_KEY`，然后：

```bash
    --judge deepseek        # -> deepseek/deepseek-chat via LiteLLM
```

这**不是**与头条运行相同的评分器：`deepseek-chat` 现在提供的是 DeepSeek 的 v4-flash，而非为已提交日志评分的 v3.2。不同的评分器会改变绝对分数，因此一次新运行无法与上文的 83.6% 直接比较。在单次运行内，每个系统仍共享同一个评分器，因此运行*内部*的比较仍逐项可比 —— 而这正是此评测框架实际保证的特性。在新评分器下对已提交的 500 题日志重新评分，是恢复逐一对应头条数字的唯一方式；在那之前，83.6% 仍作为由其旁所注评分器产生的数字成立。

值得直白点名，因为这是一道"可复现评测框架"护城河必须挺过的失败模式：代码保持正确、原始日志保持提交，但一个第三方模型 id 在文档化命令之下被悄然下线，而仓库中没有任何东西察觉。将评分器绑定到我们无法控制的 endpoint，是 Bet D 一项长期存在的隐患。

---

## 测量完整性注记（我们为让数字诚实而修复的问题）

以下是那类会悄悄抬高或压低记忆基准数字的错误。我们记录了自己的这些：

1. **Lean，而非完整历史（诚实的测试）：** 早先的头条（`engram_full`）将事实置于*整个*对话历史之上（约 79k token）。该系统*包含*了完整上下文，因此实际上不可能输给它 —— 它无法验证记忆架构。头条现改为 `engram_lean`，它检索一块约 9.6k-token 的切片，才是对检索主张真正的测试。
2. **全上下文截断缺陷（压低的是基线，不是我们）：** 全上下文基线一度被限制在 `_S` 的 haystack 大小以下，只喂给它最旧的会话。已修复，使其获得整个 haystack。该修复之前任何"全上下文仅得 30 分"的说法都是截断造成的假象。
3. **官方评分器，而非自制的：** 早先一个通用的"信息相同？"评分器比官方 LongMemEval 评分器*更严格*，既压低了分数，又使其无法比较。我们逐字使用官方提示词。
4. **拒答处理：** `_abs` 题目由官方*不可回答*评分器评级。
5. **可靠性：** LLM 客户端采用带抖动的指数退避重试 + 瞬时/永久错误分类；头条运行在 500 题中完成了 **0 个错误题目**。

---

## 诚实的注意事项

- **83.6% 是当前 `engram_lean` 配置的真实结果，而非精心挑选的切片。** 开发过程中小样本曾反复显得过于乐观（一个 18 项的切片曾读出 83%，而早先的完整集真相约为 58%）；**此处仅出现完整 500 题的数字。**
- lean 头条（83.6% @ 9.6k）与全上下文基线（73.2% @ 79k）都是在同一 doubao 回答器 + deepseek 评分器下的 500 题运行；全上下文基线仅依赖于（题目、回答器、评分器），三者完全相同，因此它是 lean 数字的一个公平参照。
- 最难的类别 —— 多会话推理与单会话偏好 —— 是当前的重点工作路线，在此以相同的复现准则更新。若本文件日后出现某个新数字，复现它的命令会紧邻其旁。

---

## 另一项测量：记忆是否是一个*设施*？（`eval/facility.py`）

准确性只是一个维度。一段记忆可以在 LongMemEval 上得分很高，却仍是无人问津的 store。这第二个问题在仓库内有自己的测量工具，以相同方式运行并遵循相同规则 —— 无法复现的数字不存在：

```bash
python eval/facility.py --days 7 --url http://127.0.0.1:8766 --key-file ~/.mnemotrace/watch.key
```

它只读取本地信号（agent 转录文本、watcher 的状态与日志，以及服务端的 **content-free** endpoint），不使用 LLM，并在约 20 秒内报告一个五级阶梯：

| Rung | Question | How it is decided |
|---|---|---|
| L0 存在 | 记忆是否保存了结论？ | `/v1/stats` + `/v1/memories?facts_limit=0` |
| L1 已连接 | 已结束的会话是否真正被关闭，以及有多快？ | 一段已结束的转录文本**仅当**其 session id 出现在 `GET /v1/sessions` 中时才算作已关闭；滞后仅来自 `fed` 终止的 watcher-log 块以及 hook 自身的 `lag_s` |
| L2 被读取 | 是否有人调用 `mnemotrace_recall`？ | 在剥离 `mcp__<server>__` 前缀后做结构化的 `tool_use` / `function_call` 匹配 —— 绝不做子串扫描（那会将散文提及多算约 74×）；调用按 call id 去重，因为恢复一个会话会写入新的转录文本、原样重放旧的 `tool_use` 块（此处实测：182 次原始调用，110 次去重后） |
| L3 有用 | （i）结论是否会相互取代？（ii）某次 recall 是否改变了答案？ | （i）已测量；触达上限会同时连接**两种**结果形态 —— Claude 的 `tool_result` 与 Codex 的 `function_call_output` —— 因为只连接一种会把另一个 agent 的 recall 卡在分母里（该缺陷曾读出 26%，而真相是 100%）；（ii）打印 **NOT MEASURED** —— 它需要人工标注，且不提供任何机器代理，也没有 LLM 评分器 |
| L4 被依赖 | 把它关掉一周会不会造成损害？ | **无法由机器测量**；按此打印 |

两项特性是刻意为之。**仅统计计数与比率：** 输出 schema 中没有任何字段、表格中没有任何一行可以容纳一条事实、一个查询、一个 session id、一个项目名或一个文件路径 —— 该评测框架读取的是个人转录文本，因此它必须无法引用它们。而 **`--assert-no-regress` 将退出码 1（设施发生回归）与退出码 2（测量工具自身损坏）区分开来** —— 零控制工具调用、不可达的服务端、空根目录 —— 通过一个常驻的控制行与一个过期匹配器触发器，因为混淆二者正是设施指标在仍打印零值的同时悄然死去的方式。每次运行向 `results/facility.jsonl` 追加一行，因此趋势是一个 `tail`，而非 join。

来自该评测框架的数字是按机器独立的，此处不予发布。
