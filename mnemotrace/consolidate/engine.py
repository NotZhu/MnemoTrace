"""System-2 编排：处理待整合的 episode -> 提取 -> 消解冲突 -> 存储 fact + 构建双时间轴图。
在关键路径之外运行（生产环境中异步 / 休眠期执行）。"""
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
from .summarizer import ProfileBuilder

_PROCEDURAL_EXTRACTED_PREDS = {"procedure", "how_to", "routine"}


class ConsolidationEngine:
    def __init__(
        self,
        fact_store: VectorStore,
        graph: GraphStore,
        embedder: Embedder,
        config: Config,
        llm: Optional[LLM] = None,
    ) -> None:
        self.fact_store = fact_store
        self.graph = graph
        self.embedder = embedder
        self.config = config
        self.llm = llm
        if llm is not None:
            from .llm_extractor import LLMExtractor

            self.extractor = LLMExtractor(llm, llm_topics=config.llm_topics,
                                          preference_object_normalization=config.preference_object_normalization)
        else:
            self.extractor = RuleExtractor(
                config.explicit_preference_extraction,
                config.preference_object_filter,
                config.preference_object_normalization,
                config.preference_reversal_extraction,
            )
        self.graph_builder = GraphBuilder(graph, embedder)
        # 语义冲突检测需要真实的（语义）embedder；离线 HashingEmbedder 产出的余弦值没有意义，
        # 所以在该场景下关闭它 → 只走 exact-slot，完全确定。
        from ..embed import HashingEmbedder

        sem_embedder = None if isinstance(embedder, HashingEmbedder) else embedder
        self.conflict = ConflictResolver(
            sem_embedder, config.conflict_sim_threshold,
            llm=llm if config.conflict_llm_escalation else None)
        self.profiles = ProfileBuilder()

    def consolidate(self, episodes: list[Episode]) -> dict[str, int]:
        stats = {"facts_added": 0, "duplicates": 0, "invalidated": 0}
        chrono = sorted(episodes, key=lambda e: (e.event_time, e.ingested_at))

        # Step 1: 并行提取所有 episode 的 fact——每次提取是独立的 LLM 调用（无共享状态）。
        # 并发上限 4：当多个 bench worker 同时调用时，8 路并发意味着约 24 个并发的 relay 调用
        # → 触发限流/退避，整体反而更慢。4 能让 relay 保持健康，同时把 8 次串行等待压缩到约 2 轮。
        # 默认上限为 4（多个 bench worker 并发时保持 relay 健康）；像向 console 灌入整个数据集
        # 这类一次性批量任务，可通过 MNEMOTRACE_EXTRACT_WORKERS 调高——那时只有这一个调用方，
        # relay 扛得住额外的扇出。
        import os
        _xw = int(os.environ.get("MNEMOTRACE_EXTRACT_WORKERS", "4"))
        ep_facts: dict[str, list] = {}
        # 提取器支持时走两阶段：模型调用先扇出（无状态），随后按时间顺序把结果转成 Fact。
        # 身份注册在第二阶段，依赖顺序——放在线程池里做会让规范名取决于哪个调用先返回，导致
        # 同一 episode 这次产出 "Evan ..."、下次产出 "user ..."，相同运行之间悄然改变 fact 集。
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

    def profile(self, user_id: str) -> dict[str, str]:
        subject = self.self_name(user_id)
        live = [f for f in self.fact_store.values() if f.user_id == user_id and f.is_live()]
        return self.profiles.build(subject, live)
