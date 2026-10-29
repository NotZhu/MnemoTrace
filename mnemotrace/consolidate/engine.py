"""System-2 记忆整合：从事件中抽取事实、处理冲突并更新事实存储和图关系。

整合由调用方触发，可同步执行，也可由服务配置为异步执行。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from typing import Optional

from ..config import Config
from ..embed import Embedder
from ..llm import LLM
from ..store import GraphStore, VectorStore
from ..types import Episode, WorkingMemory
from ..extraction import require_llm
from ..util import DAY
from .conflict import ConflictResolver
from .decay import is_durable
from .preferences import (
    EXPLICIT_PREFERENCE_PREDS,
    is_preference_reversal_fact,
    is_specific_preference_object,
    preference_topic_object,
)
from .graph_builder import GraphBuilder

_PROCEDURAL_EXTRACTED_PREDS = {"procedure", "how_to", "routine", "instruction", "agent_instruction"}


class ConsolidationEngine:
    """按事件时间抽取、消解并存储事实，同时维护图关系。"""
    def __init__(
        self,
        fact_store: VectorStore,
        graph: GraphStore,
        embedder: Embedder,
        config: Config,
        llm: Optional[LLM] = None,
        *,
        working_mem: Optional[dict[str, WorkingMemory]] = None,
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
        from .llm_extractor import LLMExtractor
        self.extractor = LLMExtractor(llm, llm_topics=config.llm_topics,
                                      preference_object_normalization=config.preference_object_normalization)
        self.working_mem = working_mem if working_mem is not None else {}
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
        stats = {"facts_added": 0, "duplicates": 0, "invalidated": 0, "working_added": 0}
        chrono = sorted(episodes, key=lambda e: (e.event_time, e.ingested_at))

        if not chrono:
            return stats
        require_llm(self.llm)
        # Validate the entire batch before changing facts, identities, or completion flags.
        import os
        workers = max(1, int(os.environ.get("MNEMOTRACE_EXTRACT_WORKERS", "4")))
        with ThreadPoolExecutor(max_workers=min(workers, len(chrono))) as pool:
            futures = {ep.id: pool.submit(self.extractor.raw_items, ep) for ep in chrono}
            raw = {ep.id: futures[ep.id].result() for ep in chrono}
        staged = copy(self.extractor)
        staged.self_name = deepcopy(self.extractor.self_name)
        staged.aliases = deepcopy(self.extractor.aliases)
        ep_facts, identity_updates, working = {}, {}, {}
        counts = {ep.id: {"facts_added": 0, "working_added": 0} for ep in chrono}
        for ep in chrono:
            before = (staged.self_name.get(ep.user_id), set(staged.aliases.get(ep.user_id, ())))
            ep_facts[ep.id] = staged.facts_from(ep, raw[ep.id]["facts"])
            identity_updates[ep.id] = before != (
                staged.self_name.get(ep.user_id), set(staged.aliases.get(ep.user_id, ())))
            working[ep.id] = [WorkingMemory(
                content=item["content"].strip(), kind=item["kind"], user_id=ep.user_id,
                session_id=ep.session_id, event_time=ep.event_time,
                metadata={"episode_id": ep.id}) for item in raw[ep.id]["working"]]
            for wm in working[ep.id]:
                wm.embedding = self.embedder.embed(wm.content)
        for ep in chrono:
            accepted = []
            for fact in ep_facts[ep.id]:
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
                accepted.append(fact)
            ep_facts[ep.id] = accepted

        # Step 2: 按时间顺序消解冲突——顺序对 "supersedes" 链很重要。
        for ep in chrono:
            for fact in ep_facts.get(ep.id, []):
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
                counts[ep.id]["facts_added"] += 1

        # Storage failures may leave facts to reconcile on retry. Keep every episode pending
        # until its working entries and identities can also be committed.
        self._decay_sweep()
        seen_working = {(w.metadata.get("episode_id"), w.content, w.kind)
                        for w in self.working_mem.values()}
        for ep in chrono:
            for wm in working[ep.id]:
                key = (ep.id, wm.content, wm.kind)
                if key not in seen_working:
                    self.working_mem[wm.id] = wm
                    seen_working.add(key)
                    counts[ep.id]["working_added"] += 1
            stats["working_added"] += counts[ep.id]["working_added"]
        self.extractor.self_name = staged.self_name
        self.extractor.aliases = staged.aliases
        for ep in chrono:
            durable = bool(ep_facts[ep.id]) or identity_updates[ep.id]
            ep.consolidated = True
            ep.metadata.pop("extraction_error", None)
            ep.metadata["extraction_counts"] = counts[ep.id]
            ep.metadata["memory_result"] = (
                "mixed" if durable and working[ep.id] else
                "working" if working[ep.id] else "long" if durable else "none")
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
