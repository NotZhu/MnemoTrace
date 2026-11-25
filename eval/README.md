# 正式质量评测

指南面向从仓库运行评测并归档公开结果的维护者。评测依赖独立于产品安装。

```sh
uv sync --locked --group eval
uv run --locked python -m eval --help
```

## 数据与配置

按 [数据锁](dataset-lock.json) 下载固定 revision 的原始文件，保留清单中的相对路径。校验通过且题量与数据锁一致后才会进入选题。

| 基准 | 上游 | 本地输入 |
| --- | --- | --- |
| LongMemEval-S | [cleaned 发布](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | longmemeval_s_cleaned.json |
| LoCoMo | [ACL 2024 发布](https://github.com/snap-research/locomo) | locomo10.json，对应上游 data/ 下的文件 |
| PersonaMem v2 | [文本发布](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2) | benchmark/text/benchmark.csv，以及数据锁所列的 200 份 data/chat_history_32k/ 文件 |

以 [LongMemEval 配置](longmemeval.example.toml) 为模板，填入实际服务、镜像或代码版本、模型和 tokenizer。数据路径相对于配置文件。LoCoMo 使用 benchmark = "locomo"，PersonaMem 使用 benchmark = "personamem-v2" 且可省略裁判。凭据只从环境变量读取。MnemoTrace 使用独立评测账号。

回答或裁判直连 DeepSeek 官方接口时，可在对应模型段配置 `disable_thinking = "deepseek"`。LongMemEval 更换裁判模型时，报告记录实际裁判型号和所沿用的上游模板。

MnemoTrace、全文输入和原文 BM25 共用回答配置。全文组按实际问题、历史和输出余量预检窗口。召回组默认使用 20 个候选和 4,096 token 的证据预算。查询状态固定为 track_usage=false，主协议不为召回额外传入 as_of。对没有时区的原始日期使用 UTC，PersonaMem 的历史 system 文本作为标记后的原始记录传给记忆后端。配置的 tokenizer 用于同一预算和预检，响应中的 usage 用于计费统计。首次使用某些 tiktoken 编码可能需要下载其校验过的编码表，之后可复用本地缓存。

## 命令

```sh
uv run --locked python -m eval prepare --config eval/longmemeval.example.toml
uv run --locked python -m eval run --config eval/longmemeval.example.toml
uv run --locked python -m eval resume --run-dir .artifacts/eval/<run_id>
uv run --locked python -m eval validate --run-dir .artifacts/eval/<run_id>
uv run --locked python -m eval validate --run-dir .artifacts/eval/<run_id> --for-publication
uv run --locked python -m eval report --run-dir .artifacts/eval/<run_id>
uv run --locked python -m eval cleanup --run-dir .artifacts/eval/<run_id>
```

prepare、validate 和 report 只处理本地评测数据与记录，不调用模型。run 与 resume 会访问配置中的服务，涉及真实模型或付费时须按仓库规则取得授权。小样本用 question_ids 明确指定，完整评测省略该项。resume 可用 --from-stage answer 或 --from-stage score 重跑对应阶段和后续阶段。清理后的历史若仍需召回，将由原始历史重新写入。

MnemoTrace 创建空间结果未知时，`cleanup` 按本次运行的派生名称查找空间。暂时查不到时保留登记，后续可再次清理。确认服务端创建请求已结束后，可用 `cleanup --confirm-settled` 处理仍待确认的资源。

## 指标与产物

- LongMemEval 使用 [上游裁判模板](longmemeval-judge.json) 和示例配置中的 `gpt-4o-2024-08-06` 裁判版本。裁判响应须为 `yes` 或 `no`，允许大小写、外围空白与末尾句点，其余响应等待重评。500 道题进入总体、分题型和六类题型等权平均正确率，30 道弃答题另列。等权平均在六类均有有效评分时生成。
- LongMemEval 的原始候选排名在 470 道有证据题上计算会话级检索指标。`context_evidence_coverage` 另用实际送入模型的文本统计证据覆盖。一个金标会话有任一带该来源的完整候选进入上下文，即计为覆盖。只保留部分文本的候选单列，逐题来源写入 `score.jsonl`。全文组也统计上下文覆盖，来源不可核验时保留缺失状态。
- LoCoMo 用本仓库固定的语义裁判提示对照参考答案。主正确率覆盖 1,540 道非对抗题，另报告 446 道对抗题。组合证据 ID 拆成独立发言 ID。仍无法映射的原始标注列入 `unmapped_evidence`，对应题目仅参加问答评分。Recall@K 检查前 K 个候选覆盖的原始发言，NDCG@K 按包含金标发言的候选位置计算。原论文的词级 F1 有不同定义。
- PersonaMem v2 按同题同顺序的四个选项计分。召回使用原始题干，选项只进入回答阶段。BM25 按发言构造最多 512 token 的原文块，同份历史复用索引。全文组使用完整历史。
- 全文输入没有检索排名。来源不能映射的召回保留问答成绩，检索指标留空。回答与裁判费用按 `events.jsonl` 中全部调用的已返回用量和配置价格估算，包含重跑及无效响应。存在未知用量时，总费用留空，`measured_cost_usd` 显示可估算部分。写入和嵌入费用留空。

运行目录包含 SQLite 原始记录、JSONL 导出、脱敏清单和 `report.json`。报告记录数据与代码哈希、配置声明的后端和模型版本、响应返回的实际回答和裁判模型名、检索预算、有效评分数及各阶段 p50/p95。清理与断点恢复使用本地 SQLite，其中保存实际连接地址和凭据变量名，适合本地保留。公开发布使用脱敏后的 `manifest.json`、JSONL 和报告，发布前人工复核原始响应和数据许可。`complete` 表示当前清单内的阶段和清理均已完成。发布使用 `validate --for-publication`，要求同次三组均达成全量覆盖、逐题结果可以重算，并且能从记录的 Git 提交还原评测源码、协议文件和依赖输入的指纹。工作区代码可以用于本地试跑，`source_verified` 显示版本核验结果。 [仓库评测规则](../AGENTS.md#验证与评测)
