"""检索、整合与存储的可调参数。精确默认值在本模块维护；算法调整通过统一评测验证。"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional


@dataclass
class Config:
    embed_dim: int = 256  # 默认 HashingEmbedder 的向量维度；显式嵌入器使用自身维度

    # --- 检索数量与候选范围 ---
    top_k: int = 5  # 未单独指定数量时，混合检索默认返回的事实数上限
    candidate_k: int = 24  # 当前未被检索流程使用，修改此值不会改变候选池
    # 默认扫描用户的全部有效事实。开启后使用向量候选和查询实体的图邻居，可能漏掉
    # 词汇匹配强但向量相似度低的事实；速度取决于向量后端，调整前应评测召回和排序。
    ann_candidates: bool = False  # 启用有界向量候选路径
    ann_pool: int = 200  # 向量候选数量，实际请求数至少为 config.top_k

    # --- 检索排序融合权重：各信号先独立排序，再用加权 RRF 融合；权重不是百分比 ---
    w_sem: float = 1.0  # 查询与记忆向量的语义相似度
    w_lex: float = 0.6  # 查询与记忆文本的词汇匹配（BM25）
    # 图权重从 0.8 降到 0.4 曾提高 dev recall@15，却降低完整集上的跨会话问答成绩；
    # 不能仅凭召回率调参，应结合端到端评测。
    w_graph: float = 0.8  # 查询实体与记忆实体的多跳图关系
    w_rec: float = 0.3  # 新近度：按事实有效时间计算，越近得分通常越高
    w_sal: float = 0.25  # 显著度：事实记录的重要程度

    # 按事实类型调整语义相似度，倾向于让相关的偏好和身份事实排在偶发信息之前。
    # 乘数作用于语义分数，而非融合后的排名分数；其他类型使用 1.0。
    # HashingEmbedder 下不启用类型加权，因为哈希相似度不是可靠的语义信号。
    w_type_preference: float = 1.25  # 偏好类事实，如 likes、favorite_food
    w_type_identity: float = 1.15  # 身份类事实，如 works_at
    recency_tau_days: float = 45.0  # 新近度指数衰减的时间常数（天）；<= 0 时不衰减
    rrf_k: int = 60  # RRF 排名融合常数，控制不同排名之间的贡献差距
    abstain_threshold: float = 0.45  # 无属性匹配时，最佳语义相似度低于此值则拒答

    # --- 事实抽取与整合 ---
    extraction_max_input_bytes: int = 16_384  # 系统与用户提示词 UTF-8 字节预算；不是模型精确 token 数
    extraction_context_bytes: int = 2_048  # 每个窗口最多携带的前文正文 UTF-8 字节数
    joint_max_output_tokens: int = 8192
    joint_core_output_tokens: int = 2048
    joint_summary_output_tokens: int = 2048
    joint_context_tokens: Optional[int] = None  # 提供方总上下文；未知时不冒充已验证
    explicit_preference_extraction: bool = True  # 抽取 like、prefer、avoid 等显式偏好
    preference_object_filter: bool = True  # 过滤 it、these ideas 等含义不明确的偏好对象
    preference_object_normalization: bool = True  # 将 sound/idea of X 等偏好对象规范化为 X
    preference_reversal_extraction: bool = True  # 抽取 no longer like X 等明确的偏好反转
    llm_topics: bool = True  # 在已有 LLM 抽取调用中生成主题；关闭后仍使用规则判定主题
    procedural_extraction: bool = True  # 将长期操作步骤抽取为过程类事实
    max_hot_facts: int = 10_000  # 热层事实数量上限；换出的事实保留在冷层
    salience_decay_per_day: float = 0.02  # 适用事实的显著度按有效时间指数衰减的日系数
    access_boost: float = 0.5  # 访问事实时增加的显著度
    # 对符合其他条件的同一主体的不同槽位事实，语义相似度达到阈值时可触发替代。
    # HashingEmbedder 不启用语义冲突路径，同槽位规则仍生效。
    conflict_sim_threshold: float = 0.80
    # 有 LLM 时，可对相似度略低于阈值的候选调用模型判断是否替代。
    conflict_llm_escalation: bool = False
    # 有 LLM 时检测疑似冲突并交由用户确认，不自动应用。
    conflict_detection: bool = False

    # 自动抽取产生的工作记忆（当前任务/意图/状态）的墙钟存活时长（秒），到点由清扫移除。
    # 设为 None 时沿用旧行为：仅随会话关闭/清除而消失，否则持续累积变旧。
    working_memory_ttl_seconds: Optional[float] = 86_400.0

    # --- 条件事件摘要 ---
    summary_min_chars: int = 2000
    summary_max_chars: int = 800
    summary_max_input_bytes: int = 16_384
    summary_max_output_tokens: int = 1024

    # --- 上下文与证据 ---
    evidence_planner: bool = True  # 根据查询意图选择 lean_context 的证据组织方式
    evidence_budgeting: bool = True  # 根据查询意图分配上下文预算
    numeric_aggregation_candidates: bool = True  # 为计数或求和查询收集金额、小时等数值证据
    aggregation_recall_expansion: bool = True  # 扩展聚合查询的证据检索范围
    aggregation_constraint_filter: bool = True  # 将不满足查询约束的数值候选标记为 EXCLUDE
    aggregation_pool_boost: bool = True  # 扩大计数、列举和求和查询的会话候选范围
    summary_fallback: bool = True  # 事实无法回答时尝试从会话摘要中作答
    summary_fallback_k: int = 6  # 摘要回退最多检查的候选数量
    procedural_memory: bool = True  # 在读取结果中提供长期规则和操作步骤
    procedural_memory_k: int = 6  # 读取过程类事实时默认检查的候选数量
    chain_evidence: bool = True  # 为检索事实附带有限长度的替代与演化链
    temporal_history_queries: bool = True  # 使用替代链处理之前、过去等历史查询
    chain_current_first: bool = True  # 演化链先展示每个槽位的当前值，再展示历史
    provenance_chunk_promotion: bool = True  # 将事实的来源事件提升为原始文本片段
    provenance_evidence: bool = True  # 为检索事实附带简洁的来源事件

    # --- 图检索与多跳规划 ---
    max_hops: int = 2  # 图遍历和多跳规划的最大跳数
    graph_proximity: bool = True  # 使用图邻近关系打分并遍历图
    graph_relation_awareness: bool = True  # 根据查询条件调整图关系类型的权重
    graph_path_reinforcement: bool = True  # 多条路径指向同一节点时提高其图得分
    graph_self_anchor: bool = True  # 将第一人称查询锚定到用户自己的图节点
    graph_entity_alias_anchor: bool = True  # 用唯一的实体名或别名词匹配图节点
    graph_negative_constraints: bool = True  # 图检索时处理 not、except 等实体排除条件
    planner_location_chains: bool = True  # 启用地点相关的多跳关系链
    planner_project_chains: bool = True  # 启用项目与地点相关的多跳关系链
    planner_llm_decomposition: bool = True  # 有 LLM 时可调用它分解多跳查询；关闭后使用规则

    @classmethod
    def from_env(cls) -> Config:
        """Apply the same deployment overrides at each task-processing entry point."""
        config = cls()
        if (topics := os.environ.get('MNEMOTRACE_LLM_TOPICS')) is not None:
            config.llm_topics = topics != '0'
        return config
