"""检索、整合与存储的可调参数。精确默认值在本模块维护；算法调整通过统一评测验证。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:

    # --- 检索数量与候选范围 ---
    top_k: int = 5  # 未单独指定数量时，混合检索默认返回的事实数上限
    candidate_k: int = 24  # Deprecated. Retained for existing benchmark configuration snapshots.
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
    recency_tau_days: float = 45.0  # 新近度指数衰减的时间常数（天）；<= 0 时不衰减
    rrf_k: int = 60  # RRF 排名融合常数，控制不同排名之间的贡献差距
    abstain_threshold: float = 0.45  # 无属性匹配时，最佳语义相似度低于此值则拒答

    # --- 显式画像刷新预算 ---
    profile_max_input_chars: int = 48_000
    profile_max_output_tokens: int = 4096

    # --- 事实抽取与整合 ---
    extraction_max_input_bytes: int = 16_384  # 系统与用户提示词 UTF-8 字节预算；不是模型精确 token 数
    extraction_context_bytes: int = 2_048  # 每个窗口最多携带的前文正文 UTF-8 字节数
    joint_max_output_tokens: int = 8192
    joint_core_output_tokens: int = 2048
    joint_digest_output_tokens: int = 2048
    joint_context_tokens: Optional[int] = None  # 提供方总上下文；未知时不冒充已验证
    max_hot_facts: int = 10_000  # 热层事实数量上限；换出的事实保留在冷层
    salience_decay_per_day: float = 0.02  # 适用事实的显著度按有效时间指数衰减的日系数
    access_boost: float = 0.5  # 访问事实时增加的显著度
    # Threshold for cross-slot candidate recall with a semantic embedder.
    conflict_sim_threshold: float = 0.80


    # --- 条件事件摘要 ---
    digest_min_chars: int = 2000
    digest_max_chars: int = 800
    digest_max_input_bytes: int = 16_384
    digest_max_output_tokens: int = 1024

    # --- 上下文与证据 ---
    evidence_budgeting: bool = True  # 按证据块优先级分配上下文预算
    digest_fallback: bool = True  # 事实无法回答时尝试从会话摘要中作答
    digest_fallback_k: int = 6  # 摘要回退最多检查的候选数量
    chain_evidence: bool = True  # 为检索事实附带有限长度的替代与演化链
    chain_current_first: bool = True  # 演化链先展示每个槽位的当前值，再展示历史
    provenance_chunk_promotion: bool = True  # 将事实的来源事件提升为原始文本片段
    provenance_evidence: bool = True  # 为检索事实附带简洁的来源事件

    # --- 图检索 ---
    max_hops: int = 2  # 混合召回中图信号的最大跳数
    graph_proximity: bool = True  # 使用图邻近关系打分并遍历图
    graph_relation_awareness: bool = True  # 根据查询条件调整图关系类型的权重
    graph_path_reinforcement: bool = True  # 多条路径指向同一节点时提高其图得分

    @classmethod
    def from_env(cls) -> Config:
        """Return the shared application defaults. Deployment adapters load their own environment settings."""
        return cls()
