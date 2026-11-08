"""Apply normalized facts and their projections; never call a model or schedule work."""
from __future__ import annotations
import math
from ..util import DAY
from .decay import is_durable
from .graph_builder import GraphBuilder
from .preferences import EXPLICIT_PREFERENCE_PREDS, is_preference_reversal_fact, is_specific_preference_object, preference_topic_object
_PROCEDURAL_EXTRACTED_PREDS = {"procedure", "how_to", "routine", "instruction", "agent_instruction"}

class FactWriter:
    def __init__(self, fact_store, graph, embedder, config):
        self.fact_store, self.config = fact_store, config
        self.graph_builder = GraphBuilder(graph, embedder)

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
