# 配置参考

配置字段及默认值以 [Config](../../mnemotrace/config.py)、服务初始化和 CLI 的 `--help` 为准。本页解释影响面，运行实验时仍需保存实际参数。

## 存储与嵌入

| 选择 | 用途与边界 |
| --- | --- |
| `storage="memory"` | 默认内存实现，离线闭环零依赖 |
| `storage="durable"` | JSONL + manifest 状态快照，运行时 store 仍在内存 |
| `storage="lancedb"` | 可选持久向量后端；安装对应 extra，文档和图仍用快照 |
| `data_path` | 可选后端所用数据目录，服务自身使用安全 namespace 目录 |
| `hashing` | 确定性离线嵌入，无需模型下载；不能假定其具备真实语义能力 |
| `bge-*` / embedding API | 真实嵌入，语言和维度能力需适配语料 |

```python
from mnemotrace import Config, Memory

memory = Memory.open("./my-store", config=Config(storage="durable"))
memory.add("My name is Alex.", user_id="user123")
memory.consolidate()
memory.save("./my-store")
```

远端 `LiteLLMEmbedder` 的默认 `BAAI/bge-m3` 为 1024 维；`openai:` 是工厂前缀，`openai/` 是 LiteLLM 路由。其他远端模型必须显式给出正整数维度，首次请求前确定，逐条校验返回值。`OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 是模型凭据，与服务 Bearer `key` 不同。

manifest 绑定模型身份与维度。不匹配时先备份并显式 `reembed`；仅 `MNEMOTRACE_REEMBED_ON_MISMATCH=1` 允许服务加载时重嵌入，不能静默混用向量空间。

## 检索与证据

| 参数组 | 作用 |
| --- | --- |
| `w_sem/w_lex/w_graph/w_rec/w_sal`、`rrf_k` | 各信号排名的加权 RRF；权重不作用于不同量纲的原始分数 |
| `top_k/candidate_k`、`ann_candidates/ann_pool` | 返回与候选规模；ANN 默认关闭，开启前验证词法强、向量弱事实的召回 |
| `max_hops`、`graph_*`、`planner_*` | 图邻近、实体锚定、负约束与多跳；LLM 分解在有可用模型时增加读取调用 |
| `abstain_threshold` | 属性依据不足时的弃答门控 |
| `evidence_planner/evidence_budgeting` | 证据需求与预算分配 |
| `chain_evidence/chain_current_first/temporal_history_queries` | 有界替代链、当前值优先及历史问题 |
| `provenance_chunk_promotion/provenance_evidence` | 从事实来源补回原始会话 |
| `summary_fallback/procedural_memory` | 无足够事实时的有来源摘要与规程证据 |
| `numeric_aggregation_candidates/aggregation_recall_expansion/aggregation_constraint_filter` | 数值候选、高召回子查询与局部约束 |
| `aggregation_pool_boost` | 聚合问题扩大整合证据池，成本和独立收益需分别评测 |

`lean_context` 的 `n_facts`/`n_summaries`/`n_chunks`/`char_budget` 是调用参数；harness 的 `topk`、extract-k 等也可能覆盖核心配置。内部衰减倍率和去重阈值不等同于公开 `Config` 字段，位置见[读取架构](../architecture/retrieval.md)。

## 整合与会话

显式偏好抽取、对象过滤/规范化、偏好反转及 `procedural_extraction` 分别控制提取路径。`salience_decay_per_day`、`access_boost` 控制衰减与强化；`conflict_sim_threshold` 控制语义候选，`conflict_llm_escalation/conflict_detection` 默认关闭。服务层只有在 `MNEMOTRACE_CONFLICT_DETECTION=1` 且配置了 LLM 时才开启候选冲突检测；它与明确更新的规则失效不同。

`session_outcomes` 属于 `close_session` 生命周期，启用 LLM 后默认可蒸馏结论；`MNEMOTRACE_SESSION_OUTCOMES=0` 强制关闭。标准 benchmark 不调用会话关闭，不能以其成绩验证结论蒸馏。

## 服务环境

| 环境项 | 作用 |
| --- | --- |
| `MNEMOTRACE_API_KEYS` / `MNEMOTRACE_SPACES` | 凭据映射与 `home/read/write` 空间授权 |
| `MNEMOTRACE_OPEN` / `MNEMOTRACE_ALLOW_ANONYMOUS` | 显式开放与额外匿名开关，生产默认受保护 |
| `MNEMOTRACE_DATA_DIR` | 服务数据根目录 |
| `MNEMOTRACE_EMBEDDER` / `MNEMOTRACE_LLM` | 服务嵌入与抽取模型 |
| `MNEMOTRACE_ANSWERER` / `MNEMOTRACE_VISION_LLM` | 可单独配置回答与视觉模型；省略时复用主 LLM |
| `MNEMOTRACE_CONFLICT_DETECTION` / `MNEMOTRACE_SESSION_OUTCOMES` | 显式开启候选冲突检测 / 强制关闭会话结论蒸馏的服务开关 |
| `MNEMOTRACE_REEMBED_ON_MISMATCH` | 显式允许模型不匹配时重嵌入 |

完整部署设置和请求限制见 [deploy](../../deploy/README.md)，凭据文件、模型接口与可选 extra 的边界见[服务架构](../architecture/service.md)。不把尚未实现的后端写成可用配置。
