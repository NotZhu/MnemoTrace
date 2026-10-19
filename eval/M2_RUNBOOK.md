# M2 运行手册 —— 竞品、第二个基准、多骨干

> **Judge 端点变更（2026-08-27）**：`volcano:deepseek-v3-2-251201` 已被 Volcano Ark 标记为 `status: Shutdown`（该平台整个 `deepseek-*` 族均已下线），本文档中的 judge 已换为仍在服务的 `deepseek`（DeepSeek 官方 API）。`doubao-seed-1-6-flash-250615` 当前为 `status: Retiring`，需留意后续替换。换 judge 会改变绝对分数，与已公布的 83.6 / 73.2 不可直接比较；同一次 run 内的系统间对照仍然有效。详见 `RESULTS.md`。

这三项实验将论文从"workshop-grade"提升至"top-venue competitive"（见论文的 Limitations 一节）。**这里的每个数字都必须来自真实运行 —— 绝不可捏造。**三项最终都归结为运行 `eval/bench.py`，它把*相同*的回答器 + 评分器应用到每个系统，因此任何运行内比较在构造上都是公平对照。

> ⚠️ **门禁。** 这些运行会发起真实的 LLM 调用（每个记忆系统自身的抽取**加上**共享的回答器 + 评分器，×500 个问题 × #systems × #backbones）。这是真实的 API 开销和数小时的墙钟时间，且需要离线开发环境中**没有**的 API key 与数据集。先设置 key，再运行。逐题原始日志落在 `--out`；用 `eval/report.py` 和 `paper/compute_stats.py` 将其并入论文。

## 密钥（按需设置各运行所需）

| 环境变量 | 用途 |
|---|---|
| `ARK_API_KEY` (+ `ARK_BASE_URL`) | 当 `--answerer volcano:…` / `doubao:…` 时的回答器/抽取器 |
| `DEEPSEEK_API_KEY` | DeepSeek 评分器**以及** Mem0 的内部 LLM |
| `OPENAI_API_KEY` | HippoRAG（OpenIE）与 Graphiti 的抽取 LLM |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | Zep/Graphiti 的图后端（运行中的 Neo4j 或 FalkorDB） |
| `HIPPORAG_LLM` | HippoRAG 的 LLM 名称（默认 `gpt-4o-mini`） |

`engram_lean` / `full_context` 自身只需要共享的回答器+评分器+抽取器 key（+ 本地 `bge-small` 嵌入器，无需 key）。

---

## 1. 同一评测框架上的竞品  ✅ 代码就绪

适配器以黑盒（`context(item)->str`）形式存在于 `eval/bench.py`，注册于 `SYSTEMS`：

| 系统 | 状态 | 运行所需额外配置 |
|---|---|---|
| `mem0` | **已接线 + 已安装包** | `DEEPSEEK_API_KEY`（Mem0 的内部 LLM） |
| `zep` | 已添加适配器 —— **首次带 key 运行时需验证** | `pip install graphiti-core`；运行中的 Neo4j/FalkorDB；`OPENAI_API_KEY` |
| `hipporag` | 已添加适配器 —— **首次带 key 运行时需验证** | `pip install hipporag`；`OPENAI_API_KEY`（+ `HIPPORAG_LLM`） |

`zep`/`hipporag` 针对各库的公开 API 编写，但无法在离线环境中执行（无 key / 无图数据库），因此请将首次带 key 的运行视为一次验证过程 —— 跨库版本的签名可能只需一行微调。

```bash
# headline competitor table (start with the ready one: MnemoTrace vs Mem0 vs full-context)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python eval/bench.py \
    --data s --limit 500 \
    --systems engram_lean,mem0,full_context \
    --answerer volcano:doubao-seed-2-0-pro-260215 \
    --judge deepseek \
    --extractor volcano:doubao-seed-1-6-flash-250615 \
    --embedder bge-small --reasoning --persona \
    --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
    --out results/lme_s_competitors_mem0.jsonl

# add zep + hipporag once their packages/backends are up:
#   --systems engram_lean,mem0,zep,hipporag,full_context  --out results/lme_s_competitors_all.jsonl

python eval/report.py results/lme_s_competitors_mem0.jsonl   # per-system, per-category table
```

## 2. 第二个基准 —— LOCOMO  ✅ 加载器就绪（已离线验证）

`eval/locomo.py` 将公开的 LOCOMO 发布转换为评测框架的条目形态 —— 每个 QA 对对应一个条目，haystack 为该样本完整的多会话对话；LOCOMO 第 5 类（对抗性）映射到 LongMemEval 的拒答。该转换已离线做单元测试（`python -m eval.locomo`）：条目形态、日期归一化（LOCOMO "1:56 pm on 8 May, 2023" → `parse_date` 友好的 `2023/05/08 13:56`）与拒答均已验证。因此运行只需数据集文件 + 常规的回答器/评分器 key。

获取数据：从 https://github.com/snap-research/locomo 下载 `locomo10.json`（`data/locomo10.json`）到 `eval/locomo10.json`，然后像 LongMemEval 一样运行：

```bash
python eval/bench.py --data locomo --limit 500 --systems engram_lean,mem0,full_context \
    --answerer volcano:doubao-seed-2-0-pro-260215 --judge deepseek \
    --extractor volcano:doubao-seed-1-6-flash-250615 --embedder bge-small --reasoning --persona \
    --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
    --out results/locomo_engram_vs_mem0.jsonl
python eval/report.py results/locomo_engram_vs_mem0.jsonl
```

## 3. 多骨干  ✅ 已支持

`make_llm`（`mnemotrace/llm/providers.py`）已可路由多个 provider，因此多骨干 == 用不同的 `--answerer`（+ 该 provider 的 key）重新运行。无需改动代码。

```bash
for A in "deepseek" "qwen-max" "openai:gpt-4o" "volcano:doubao-seed-2-0-pro-260215"; do
  python eval/bench.py --data s --limit 500 --systems engram_lean,full_context \
      --answerer "$A" --judge deepseek \
      --extractor volcano:doubao-seed-1-6-flash-250615 \
      --embedder bge-small --reasoning --persona \
      --chunks 2 --topk 15 --extract-k 8 --summ-k 28 --n-summaries 28 \
      --out "results/lme_s_backbone_$(echo $A | tr ':/' '__').jsonl"
done
```

该论点（精简的检索上下文在准确率上胜过全上下文）只有跨骨干成立时才令人信服 —— 这正是该矩阵的意义所在。
