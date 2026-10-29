"""System-2 记忆整合：从事件中抽取事实、处理冲突并更新事实存储和图关系。

整合由调用方触发，可同步执行，也可由服务配置为异步执行。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from ..config import Config
from ..embed import Embedder
from ..llm import LLM
from ..store import GraphStore, VectorStore
from ..types import Episode
from ..util import DAY
from .conflict import ConflictResolver
from .decay import is_durable
from .extractor import (
    EXPLICIT_PREFERENCE_PREDS,
    RuleExtractor,
    is_preference_reversal_fact,
    is_specific_preference_object,
    preference_topic_object,
)
from .graph_builder import GraphBuilder

_PROCEDURAL_EXTRACTED_PREDS = {"procedure", "how_to", "routine"}


class ConsolidationEngine:
    """按事件时间抽取、消解并存储事实，同时维护图关系。"""
    def __init__(
        self,
        fact_store: VectorStore,
        graph: GraphStore,
        embedder: Embedder,
        config: Config,
        llm: Optional[LLM] = None,
    ) -> None:
        """初始化事件整合引擎。

        Args:
            fact_store: 事实存储。
            graph: 图存储。
            embedder: 文本嵌入器。
            config: 配置。
            llm: LLM。
        """
        self.fact_store = fact_store
        self.graph = graph
        self.embedder = embedder
        self.config = config
        self.llm = llm
        if llm is not None:
            from .llm_extractor import LLMExtractor
            # 初始化 LLM 事实抽取器
            self.extractor = LLMExtractor(llm, llm_topics=config.llm_topics,
                                          preference_object_normalization=config.preference_object_normalization)
        else:
            # 初始化规则抽取器
            self.extractor = RuleExtractor(
                config.explicit_preference_extraction,
                config.preference_object_filter,
                config.preference_object_normalization,
                config.preference_reversal_extraction,
            )
        # 初始化图构建器
        self.graph_builder = GraphBuilder(graph, embedder)

        # 哈希向量不适合判断语义冲突；使用 HashingEmbedder 时关闭语义路径，保留其他冲突规则。
        from ..embed import HashingEmbedder
        sem_embedder = None if isinstance(embedder, HashingEmbedder) else embedder
        # 初始化冲突解析器
        self.conflict = ConflictResolver(sem_embedder, config.conflict_sim_threshold,
                                        llm=llm if config.conflict_llm_escalation else None)

    def consolidate(self, episodes: list[Episode]) -> dict[str, int]:
        """执行事实整合。

        Args:
            episodes: 要整合的 episode 列表。
        Returns:
            dict[str, int]: 整合统计信息。
        """
        # 初始化统计信息，包括新增事实数、重复事实数和被作废的事实数
        stats = {"facts_added": 0, "duplicates": 0, "invalidated": 0}
        # 按事件时间排序，确保按顺序处理
        chrono = sorted(episodes, key=lambda e: (e.event_time, e.ingested_at))

        # Step 1: 并行提取所有 episode 的 fact——每次提取是独立的 LLM 调用，可安全并发。
        # 并发数默认 4：太高会让模型 API 被打满而触发限流，重试反而更慢；
        # 4 既能保持顺畅，又把 8 个待处理项压到约 2 轮跑完。
        # 批量大导入（前方只有这一个调用方时）可用 MNEMOTRACE_EXTRACT_WORKERS 调高。
        import os
        # 并发数上限
        _xw = int(os.environ.get("MNEMOTRACE_EXTRACT_WORKERS", "4"))
        # 初始化事实存储
        ep_facts: dict[str, list] = {}
        # 提取器支持时，先并发调用模型，再按事件时间顺序生成事实并注册用户身份。
        # 避免模型返回顺序改变规范名，使同一输入的事实主体不稳定。
        two_phase = hasattr(self.extractor, "raw_items") and hasattr(self.extractor, "facts_from")
        if len(chrono) > 1 and two_phase:
            raw: dict[str, list] = {}
            with ThreadPoolExecutor(max_workers=min(_xw, len(chrono))) as pool:
                futs = {pool.submit(self.extractor.raw_items, ep): ep for ep in chrono}
                for fut in as_completed(futs):
                    ep = futs[fut]
                    try:
                        raw[ep.id] = fut.result()
                    except Exception:  # noqa: BLE001
                        raw[ep.id] = []
            for ep in chrono:  # 按时间顺序 => 规范名是用户实际先说出的那个
                try:
                    ep_facts[ep.id] = self.extractor.facts_from(ep, raw.get(ep.id, []))
                except Exception:  # noqa: BLE001
                    ep_facts[ep.id] = []
        elif len(chrono) > 1:
            with ThreadPoolExecutor(max_workers=min(_xw, len(chrono))) as pool:
                futs = {pool.submit(self.extractor.extract, ep): ep for ep in chrono}
                for fut in as_completed(futs):
                    ep = futs[fut]
                    try:
                        ep_facts[ep.id] = fut.result()
                    except Exception:  # noqa: BLE001
                        ep_facts[ep.id] = []  # 提取失败 → 该 episode 不产出 fact
        else:
            for ep in chrono:
                ep_facts[ep.id] = self.extractor.extract(ep)

        # Step 2: 按时间顺序消解冲突——顺序对 "supersedes" 链很重要。
        for ep in chrono:
            for fact in ep_facts.get(ep.id, []):
                if (
                    not self.config.procedural_extraction
                    and fact.predicate.lower() in _PROCEDURAL_EXTRACTED_PREDS
                ):
                    continue
                if (
                    not self.config.explicit_preference_extraction
                    and fact.predicate.lower() in EXPLICIT_PREFERENCE_PREDS
                ):
                    continue
                if (
                    not self.config.preference_reversal_extraction
                    and is_preference_reversal_fact(fact, ep.content)
                ):
                    continue
                if (
                    self.config.preference_object_filter
                    and fact.predicate.lower() in EXPLICIT_PREFERENCE_PREDS
                    and not is_specific_preference_object(fact.object)
                ):
                    continue
                if (
                    self.config.preference_object_normalization
                    and fact.predicate.lower() in EXPLICIT_PREFERENCE_PREDS
                ):
                    normalized = preference_topic_object(fact.predicate, fact.object,
                                                          self.config.preference_object_normalization)
                    if normalized != fact.object:
                        fact.object = normalized
                        fact.text = f"{fact.subject} {fact.predicate.replace('_', ' ')} {fact.object}".strip()
                fact.embedding = self.embedder.embed(fact.text)
                live = [f for f in self.fact_store.values() if f.user_id == fact.user_id and f.is_live()]
                action, invalidated = self.conflict.reconcile(fact, live)
                for old in invalidated:
                    self.graph_builder.invalidate(old.id, fact.created_at)
                    self.fact_store.upsert(old.id, old.embedding or [], old)
                    stats["invalidated"] += 1
                if action == "duplicate":
                    stats["duplicates"] += 1
                    continue
                self.fact_store.upsert(fact.id, fact.embedding, fact)
                self.graph_builder.add_fact(fact)
                stats["facts_added"] += 1
            ep.consolidated = True

        # Step 3: 显著度遗忘清扫（Bet E）。未被强化的偶发 fact 随时间淡出，使热集在规模化时保持
        # 小，陈旧的一次性内容排在持久知识之后。持久（偏好/身份）和已被强化的 fact 豁免；下限保证
        # 淡出的 fact 仍可检索（存活，只是被降权），多证据召回不会被破坏。
        self._decay_sweep()
        return stats

    def _decay_sweep(self) -> None:
        live = [f for f in self.fact_store.values() if f.is_live()]
        if not live:
            return
        now_t = max(f.valid_at for f in live)  # 已知的最晚世界时间 = 该 store 的"现在"
        for f in live:
            if is_durable(f.predicate) or f.access_count > 0:
                continue  # 用户是谁/喜欢什么，以及已被使用过的内容，不淡出
            days = max(0.0, (now_t - f.valid_at) / DAY)
            f.salience = max(0.3, math.exp(-self.config.salience_decay_per_day * days))

    def self_name(self, user_id: str) -> str:
        return self.extractor.self_of(user_id)
