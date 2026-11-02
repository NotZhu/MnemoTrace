"""Apply normalized facts and their projections; never call a model or schedule work."""
from __future__ import annotations
from copy import deepcopy
import math
from ..util import DAY
from .conflict import ConflictResolver, insert_dialogue_history
from .decay import is_durable
from .graph_builder import GraphBuilder
from .preferences import EXPLICIT_PREFERENCE_PREDS, is_preference_reversal_fact, is_specific_preference_object, preference_topic_object
_PROCEDURAL_EXTRACTED_PREDS = {"procedure", "how_to", "routine", "instruction", "agent_instruction"}

class FactWriter:
    def __init__(self, fact_store, graph, embedder, config, extra_fact_stores=()):
        self.fact_store, self.graph, self.embedder, self.config = fact_store, graph, embedder, config
        self.extra_fact_stores = extra_fact_stores
        self.graph_builder = GraphBuilder(graph, embedder)
        from ..embed import HashingEmbedder
        self.conflict = ConflictResolver(None if isinstance(embedder, HashingEmbedder) else embedder,
                                         config.conflict_sim_threshold)

    @staticmethod
    def empty_stats():
        return {"facts_added": 0, "duplicates": 0, "invalidated": 0, "working_added": 0,
                "model_calls": 0, "windows_completed": 0}

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

    def _ensure_projection(self, fact):
        subject = self.graph.get_entity(fact.user_id, fact.subject)
        if subject is None or not any(r.fact_id == fact.id for r in self.graph.neighbors(subject.id)):
            self.graph_builder.add_fact(fact)

    def _write_fact(self, fact, stats) -> int:
        self.conflict.sim_threshold = self.config.conflict_sim_threshold
        stores = (self.fact_store, *self.extra_fact_stores)
        def write(record):
            target = next((store for store in stores if store.get(record.id) is not None), self.fact_store)
            target.upsert(record.id, record.embedding or [], record)
        existing = next((record for store in stores if (record := store.get(fact.id)) is not None), None)
        if existing is not None:
            if existing.source == "user":
                stats["duplicates"] += 1
                return 0
            # A checkpoint retry repairs projection; it must not reset intervening metadata edits.
            fact = deepcopy(existing)
        records = [deepcopy(f) for store in stores for f in store.values()
                   if f.user_id == fact.user_id and f.id != fact.id]
        history = insert_dialogue_history(fact, records)
        if history is not None:
            action, updated = history
            for record in updated:
                write(record)
                self.graph.delete_relations_for_fact(record.id)
                self.graph_builder.add_fact(record)
            stats['duplicates' if action == 'duplicate' or existing else 'facts_added'] += 1
            return int(action != 'duplicate' and existing is None)
        live = [f for f in records if f.is_live()]
        action, invalidated = self.conflict.reconcile(fact, live)
        if action == "duplicate" and existing is None:
            for old in live:
                if (old.slot == fact.slot and old.assertion_kind == fact.assertion_kind
                        and old.object.strip().lower() == fact.object.strip().lower()):
                    if old.source == "extracted":
                        old.provenance = list(dict.fromkeys([*old.provenance, *fact.provenance]))
                        write(old)
                        self._ensure_projection(old)
                    break
            stats["duplicates"] += 1
            return 0
        if existing is not None:
            fact.supersedes = existing.supersedes or fact.supersedes
        # Persist the new chain head before invalidations, so interrupted writes are retryable.
        write(fact)
        for old in invalidated:
            self.graph_builder.invalidate(old.id, fact.valid_at)
            write(old)
            stats["invalidated"] += 1
        if fact.supersedes:
            self.graph_builder.invalidate(fact.supersedes, fact.valid_at)
        self._ensure_projection(fact)
        stats["duplicates" if existing else "facts_added"] += 1
        return int(existing is None)

    def _decay_sweep(self) -> None:
        live = [f for f in self.fact_store.values() if f.is_live() and f.valid_at is not None]
        if not live:
            return
        now_t = max(f.valid_at for f in live)  # 已知的最晚世界时间 = 该 store 的"现在"
        for f in live:
            if is_durable(f.predicate) or f.access_count > 0:
                continue  # 用户是谁/喜欢什么，以及已被使用过的内容，不淡出
            days = max(0.0, (now_t - f.valid_at) / DAY)
            f.salience = max(0.3, math.exp(-self.config.salience_decay_per_day * days))
            self.fact_store.upsert(f.id, f.embedding or [], f)
