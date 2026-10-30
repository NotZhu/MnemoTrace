"""System-2 记忆整合：从事件中抽取事实、处理冲突并更新事实存储和图关系。

整合由调用方触发，可同步执行，也可由服务配置为异步执行。
"""
from __future__ import annotations

import math
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import dataclass
from typing import Optional

from ..config import Config
from ..embed import Embedder
from ..llm import LLM
from ..store import GraphStore, VectorStore
from ..types import Episode, WorkingMemory
from ..extraction import ExtractionError, require_llm
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
from .processing import EpisodeProcessingStore, PROGRESS_KEY
from .windows import ExtractionWindow, SourceSpan, chronology, plan_windows
from .topic_rules import log_topic_inputs

_PROCEDURAL_EXTRACTED_PREDS = {"procedure", "how_to", "routine", "instruction", "agent_instruction"}


@dataclass(frozen=True)
class PreparedExtraction:
    """Detached inputs; execution never retains an engine, Memory or database handle."""
    windows: tuple[ExtractionWindow, ...]
    extractor: object
    max_tokens: int

    def execute(self):
        import os
        if not self.windows:
            return []
        workers = max(1, int(os.environ.get("MNEMOTRACE_EXTRACT_WORKERS", "4")))
        with ThreadPoolExecutor(max_workers=min(workers, len(self.windows))) as pool:
            return list(pool.map(lambda w: self.extractor.raw_window(w, self.max_tokens), self.windows))


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
        processing: EpisodeProcessingStore,
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
        for name in ("extraction_max_input_bytes", "extraction_max_output_tokens", "extraction_context_bytes"):
            value = getattr(config, name)
            if type(value) is not int or value < (0 if name == "extraction_context_bytes" else 1):
                raise ValueError(f"{name} must be a valid integer budget")
        self.processing = processing
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
        prepared = self.prepare(episodes)
        outputs = prepared.execute()  # Validate every response before changing derived state.
        stats = self.empty_stats()
        try:
            for window, output in zip(prepared.windows, outputs):
                result = self.apply(window, output)
                for key, value in result.items():
                    stats[key] += value
        finally:
            # Checkpoints may replace records; callers must not write back earlier references.
            for ep in episodes:
                current = self.processing.documents.get(ep.id)
                if current is not None:
                    ep.metadata, ep.consolidated = current.metadata, current.consolidated
        return stats

    @staticmethod
    def empty_stats():
        return {"facts_added": 0, "duplicates": 0, "invalidated": 0, "working_added": 0,
                "model_calls": 0, "windows_completed": 0}

    def prepare(self, episodes: list[Episode], *, first_only: bool = False) -> PreparedExtraction:
        active = []
        for ep in sorted({ep.id: ep for ep in episodes}.values(), key=chronology):
            offset = self.processing.offset(ep)
            if PROGRESS_KEY in ep.metadata and offset == len(ep.content):
                ep.consolidated = True
            if not ep.consolidated:
                active.append(ep)
        if not active:
            return PreparedExtraction((), copy(self.extractor), self.config.extraction_max_output_tokens)
        require_llm(self.llm)
        history = {ep.id: ep for ep in [*self.processing.episodes(), *active]}
        try:
            windows = plan_windows(active, list(history.values()), self.processing.offset,
                                   self.extractor.window_size, self.config.extraction_max_input_bytes,
                                   self.config.extraction_context_bytes)
        except ValueError as exc:
            raise ExtractionError() from exc
        extractor = copy(self.extractor)
        extractor.self_name, extractor.aliases = {}, {}
        return PreparedExtraction(tuple(deepcopy(windows[:1] if first_only else windows)), extractor,
                                  self.config.extraction_max_output_tokens)

    def rebind(self, window: ExtractionWindow) -> ExtractionWindow | None:
        """Validate captured source identity/ranges and bind to current authoritative records."""
        bound = []
        for spans in (window.pending, window.context):
            current = []
            for span in spans:
                ep = self.processing.documents.get(span.episode.id)
                old = span.episode
                if ep is None or (ep.content, ep.user_id, ep.session_id, ep.event_time, ep.ingested_at,
                                  ep.metadata.get('memory_scope', 'long')) != (
                        old.content, old.user_id, old.session_id, old.event_time, old.ingested_at,
                        old.metadata.get('memory_scope', 'long')):
                    return None
                if spans is window.pending and (ep.consolidated or self.processing.offset(ep) != span.start):
                    return None
                current.append(SourceSpan(ep, span.start, span.end))
            bound.append(tuple(current))
        return ExtractionWindow(*bound)

    def apply(self, window, output):
        current = self.rebind(window)
        if current is None:
            raise ValueError('Extraction sources or processing ranges changed')
        stats = self.empty_stats()
        self._apply_window(current, output, stats)
        stats.update(model_calls=1, windows_completed=1)
        return stats

    def _accepted(self, fact, content: str) -> bool:
        pred = fact.predicate.lower()
        if not self.config.procedural_extraction and pred in _PROCEDURAL_EXTRACTED_PREDS:
            return False
        if pred in EXPLICIT_PREFERENCE_PREDS:
            if not self.config.explicit_preference_extraction:
                return False
            if self.config.preference_object_filter and not is_specific_preference_object(fact.object):
                return False
            if self.config.preference_object_normalization:
                obj = preference_topic_object(pred, fact.object, True)
                if obj != fact.object:
                    fact.object = obj
                    fact.text = f"{fact.subject} {pred.replace('_', ' ')} {obj}".strip()
        return self.config.preference_reversal_extraction or not is_preference_reversal_fact(fact, content)

    def _apply_window(self, window, output, stats):
        spans = {s.id: s for s in (*window.context, *window.pending)}
        pending_ids = {s.id for s in window.pending}
        pending_order = {s.id: i for i, s in enumerate(window.pending)}
        def anchor(item):
            return max((spans[k] for k in item["source_ids"] if k in pending_ids),
                       key=lambda s: (*chronology(s.episode), pending_order[s.id], s.start))
        staged = copy(self.extractor)
        staged.self_name, staged.aliases = deepcopy(self.extractor.self_name), deepcopy(self.extractor.aliases)
        facts, working, durable, transient = [], [], set(), set()
        if staged.llm_topics:
            log_topic_inputs(output.items["facts"])
        counts = {s.episode.id: {"facts_added": 0, "working_added": 0} for s in window.pending}
        for item in sorted(output.items["facts"], key=lambda i: pending_order[anchor(i).id]):
            ep = anchor(item).episode
            before = (staged.self_name.get(ep.user_id), set(staged.aliases.get(ep.user_id, ())))
            extracted = staged.facts_from(ep, [item], log_topics=False)
            if before != (staged.self_name.get(ep.user_id), set(staged.aliases.get(ep.user_id, ()))):
                durable.add(ep.id)
            for fact in extracted:
                if not self._accepted(fact, "\n".join(spans[k].episode.content[spans[k].start:spans[k].end]
                                                       for k in item["source_ids"])):
                    continue
                fact.provenance = list(dict.fromkeys(spans[k].episode.id for k in item["source_ids"]))
                identity = json.dumps([item["source_ids"], fact.slot, fact.object], ensure_ascii=False)
                fact.id = "ft_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                fact.embedding = self.embedder.embed(fact.text)
                facts.append((ep, fact))
                durable.update(spans[k].episode.id for k in item["source_ids"] if k in pending_ids)
        for item in output.items["working"]:
            ep = anchor(item).episode
            identity = json.dumps([item["source_ids"], item["content"].strip(), item["kind"]], ensure_ascii=False)
            wm = WorkingMemory(content=item["content"].strip(), kind=item["kind"], user_id=ep.user_id,
                               session_id=ep.session_id, event_time=ep.event_time,
                               id="wm_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                               metadata={"episode_id": ep.id, "source_ids": item["source_ids"],
                                         "provenance": list(dict.fromkeys(spans[k].episode.id for k in item["source_ids"]))})
            wm.embedding = self.embedder.embed(wm.content)
            working.append((ep, wm))
            transient.update(spans[k].episode.id for k in item["source_ids"] if k in pending_ids)
        # Embedding and response validation have completed before storage changes.
        for ep, fact in facts:
            counts[ep.id]["facts_added"] += self._write_fact(fact, stats)
        self._decay_sweep()
        seen = {(w.metadata.get("episode_id"), w.content, w.kind) for w in self.working_mem.values()}
        for ep, wm in working:
            key = (ep.id, wm.content, wm.kind)
            if wm.id not in self.working_mem and key not in seen:
                self.working_mem[wm.id] = wm
                seen.add(key)
                counts[ep.id]["working_added"] += 1
                stats["working_added"] += 1
        self.extractor.self_name, self.extractor.aliases = staged.self_name, staged.aliases
        self.processing.complete(window, counts, durable, transient, output.completion, output.latency_ms)

    def _ensure_projection(self, fact):
        subject = self.graph.get_entity(fact.user_id, fact.subject)
        if subject is None or not any(r.fact_id == fact.id for r in self.graph.neighbors(subject.id)):
            self.graph_builder.add_fact(fact)

    def _write_fact(self, fact, stats) -> int:
        existing = self.fact_store.get(fact.id)
        if existing is not None:
            if existing.source == "user":
                stats["duplicates"] += 1
                return 0
            # A checkpoint retry repairs projection; it must not reset intervening metadata edits.
            fact = deepcopy(existing)
        live = [deepcopy(f) for f in self.fact_store.values()
                if f.user_id == fact.user_id and f.id != fact.id and f.is_live()]
        action, invalidated = self.conflict.reconcile(fact, live)
        if action == "duplicate" and existing is None:
            for old in live:
                if old.slot == fact.slot and old.object.strip().lower() == fact.object.strip().lower():
                    if old.source == "extracted":
                        old.provenance = list(dict.fromkeys([*old.provenance, *fact.provenance]))
                        self.fact_store.upsert(old.id, old.embedding or [], old)
                        self._ensure_projection(old)
                    break
            stats["duplicates"] += 1
            return 0
        if existing is not None:
            fact.supersedes = existing.supersedes or fact.supersedes
        # Persist the new chain head before invalidations, so interrupted writes are retryable.
        self.fact_store.upsert(fact.id, fact.embedding, fact)
        for old in invalidated:
            self.graph_builder.invalidate(old.id, fact.valid_at)
            self.fact_store.upsert(old.id, old.embedding or [], old)
            stats["invalidated"] += 1
        if fact.supersedes:
            self.graph_builder.invalidate(fact.supersedes, fact.valid_at)
        self._ensure_projection(fact)
        stats["duplicates" if existing else "facts_added"] += 1
        return int(existing is None)

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
            self.fact_store.upsert(f.id, f.embedding or [], f)

    def self_name(self, user_id: str) -> str:
        return self.extractor.self_of(user_id)
