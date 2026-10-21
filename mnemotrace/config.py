"""检索融合与整合的可调参数。默认值对离线 demo 足够合理；
在评测 harness 上则按 benchmark 逐一调优（绝不拍脑袋——见 AGENTS.md 「评测、公开内容与贡献规则」）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    # --- 存储后端选择 ---
    storage: str = "memory"  # memory | durable | lancedb
    data_path: Optional[str] = None

    # --- 检索融合权重（AGENTS.md 「检索与模块」）---
    w_sem: float = 1.0  # 稠密语义
    w_lex: float = 0.6  # 词汇 / BM25
    w_graph: float = 0.8  # n-hop 图邻近 （不可下调：这是多 session 跨会话关联的承重项——
    #                       降到 0.4 使 dev recall@15 +4.1，但完整集上的 multi-session QA 暴跌
    #                       -23。recall@15 是有误导性的代理指标；图权重必须保持高位。）
    w_rec: float = 0.3  # 新近度衰减
    w_sal: float = 0.25  # 显著度 / 重要程度

    # 按类型加权的检索（MemoryScope `type_ratio`，OMEGA 的 decisions 2x 思路）：关于用户"是谁"或
    # "偏好什么"的持久 fact，应排在相关性相近的偶然提及之上。关键在于乘数作用于语义得分
    # ——不相关 fact 的语义得分约等于 0——因此它只能在已相关的候选之间重排，绝不会把离题 fact
    # 顶到最前（更早的版本乘的是融合后的排名分，结果对 "where do you work?" 错误地浮出了
    # 'favorite_language'）。刻意保持温和；仅在真实 embedder 下启用（hashing 回退的余弦是噪声）。
    w_type_preference: float = 1.25
    w_type_identity: float = 1.15

    recency_tau_days: float = 45.0
    top_k: int = 5
    candidate_k: int = 24  # 融合前每个检索器的候选池

    # 候选生成（扩展路径；AGENTS.md Bet E）。默认关闭：检索器会扫描用户的全部 live fact
    # （精确，几千条以内没问题）。开启后改为从向量库拉取有界的 ANN 候选池（∪ 查询的图邻居
    # fact）——这是面向大存储的次线性读取路径。成为默认值前须过评测门槛：ANN 候选可能丢掉
    # 词汇强 / 向量弱的 fact，所以要先在 harness 上验证排序（AGENTS.md 「评测、公开内容与贡献规则」）。当候选池覆盖
    # 全部 fact（小存储）时候选集 == 全量扫描，结果不变。
    ann_candidates: bool = False
    ann_pool: int = 200  # ann_candidates 开启时从 ANN 索引拉取的最近候选数
    rrf_k: int = 60  # RRF（Reciprocal Rank Fusion）常数
    max_hops: int = 2  # 多跳规划器深度
    max_hot_facts: int = 10_000  # 热层上限；冷 fact 持久保留，热层未命中时可换页回来
    abstain_threshold: float = 0.45  # 无属性匹配且最佳语义相似度低于此值时拒答
    evidence_planner: bool = True  # 与 benchmark 无关的查询 -> 按 evidence 形状路由 lean_context
    evidence_budgeting: bool = True  # 按意图分配上下文预算，而不是等分/粗暴截断
    explicit_preference_extraction: bool = True  # 抽取 I like/prefer/avoid 这类显式偏好 fact
    preference_object_filter: bool = True  # 抑制 "it"/"these ideas" 这类弱显式偏好宾语
    preference_object_normalization: bool = True  # 把 "sound/idea of X" 这类偏好宾语规范化为 X
    preference_reversal_extraction: bool = True  # 抽取高置信度的 "no longer like X" 式偏好更新
    numeric_aggregation_candidates: bool = True  # 为计数/求和问题抽取金额/小时/页数等数值行
    aggregation_recall_expansion: bool = True  # 把计数/求和查询扩展为高召回的证据查找
    aggregation_constraint_filter: bool = True  # 把违反查询约束的数值候选标记为 EXCLUDE
    aggregation_pool_boost: bool = True  # 计数/列举/求和类问题整合更宽的 session 池（需要每一次出现）
    procedural_extraction: bool = True  # 把持久的 runbook/操作步骤抽取为类型化的 procedure fact
    session_outcomes: bool = True  # 把每个 session 蒸馏成 decision/finding/lesson fact（每个
    #   session 一次 LLM 调用）。默认开启。"读取路径必须逐字节一致"曾是保持关闭的理由，
    #   而现在它恰恰是打开它安全性的证据：(1) eval/ 下没有任何东西调用 close_session
    #   （grep 零命中），因此任何已发布数字都不会变——且 eval/bench.py 显式传入
    #   --embedder，服务端默认值根本到不了 harness；(2) service.py 在未设置 MNEMOTRACE_LLM 时
    #   保持 self.llm = None，而 close_session 的 outcomes 分支有 `self.llm is not None`
    #   保护，因此 pytest 和 quickstart 产出零 outcome、零 LLM 调用、零新依赖——零配置不变式
    #   不受影响。做成开关只会是死 UI：控制台和 MCP 工具都经服务端默认值关闭 session，
    #   该功能关闭时它们根本触及不到。MNEMOTRACE_SESSION_OUTCOMES=0 是运维的逃生门。
    summary_fallback: bool = True  # fact 无法回答时，从派生的 session 摘要中作答
    summary_fallback_k: int = 6  # 回退在拒答前检查的候选摘要数
    procedural_memory: bool = True  # 把长期规则/操作类记忆作为类型化的派生读取层呈现
    procedural_memory_k: int = 6  # 拒答或上下文打包前检查的候选 procedure fact 数
    chain_evidence: bool = True  # 为检索到的 fact 附带有界的 supersedes/演化链
    temporal_history_queries: bool = True  # 用取代链回答 before/previous/former 类查询
    chain_current_first: bool = True  # 演化块先给每个槽位的当前值，再给历史
    provenance_chunk_promotion: bool = True  # 把检索到 fact 的来源 episode 提升为原始 chunk
    provenance_evidence: bool = True  # 为检索到的 fact 附带紧凑的原始来源 episode
    graph_proximity: bool = True  # n-hop 图邻近打分 / 遍历消融开关
    graph_relation_awareness: bool = True  # 图邻近内部按查询条件加权 predicate
    graph_path_reinforcement: bool = True  # 多条路径支持同一图节点时给予 PPR 式加成
    graph_self_anchor: bool = True  # 把第一人称/用户查询锚定到用户自己的图节点
    graph_entity_alias_anchor: bool = True  # 锚定唯一的实体名/别名 token（如 "Moonshot" -> Moonshot AI）
    graph_negative_constraints: bool = True  # 图检索中遵守 not/except/excluding 实体约束
    planner_location_chains: bool = True  # 让多跳规划器回答 based_in/located_in/headquarters 链
    planner_project_chains: bool = True  # 让多跳规划器走 works_on/project -> based_in 链
    planner_llm_decomposition: bool = True  # 让 LLM 分解多跳查询（读取路径增加一次 LLM 调用）；off => 离线关键词映射

    # --- 整合 ---
    salience_decay_per_day: float = 0.02
    access_boost: float = 0.5
    embed_dim: int = 256
    # 语义冲突检测：当同一 subject 的较新 fact 与旧 fact 的嵌入相近到这个程度
    # （同一属性、不同的自由表述 predicate）时，将旧 fact 失效化。在 harness 上调优。
    conflict_sim_threshold: float = 0.80
    # 把处于临界值的冲突（相似度略低于阈值）升级给 LLM 裁决器（「IV. 不允许静默损坏记忆」的歧义裁决）。
    # 默认关闭：让整合对每条 fact 保持零 LLM 调用（Bet C）；追求最高精度时开启。
    conflict_llm_escalation: bool = False
    # 针对模糊尾部的 System-2 LLM 冲突检测：把廉价规则枚举不出的可疑冲突作为待处理项呈现给
    # 用户确认（绝不自动解决）。默认关闭以保证离线 / 测试的确定性；生产环境可开启 detect->confirm 循环。
    conflict_detection: bool = False
