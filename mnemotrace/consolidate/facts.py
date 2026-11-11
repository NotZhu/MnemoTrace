"""Apply normalized facts and their projections; never call a model or schedule work."""
from __future__ import annotations
import math
from ..util import DAY
from ..types import Fact
from .pipeline import content_hash
from .planning import fact_valid_at, source_anchor
from .graph_builder import GraphBuilder


def materialize_facts(user_id, data, result, now):
    """Build the same facts from a saved extraction in either storage adapter."""
    views = {item['id']: item for item in (*data['context'], *data['pending'])}
    pending = {item['id']: item for item in data['pending']}
    input_hash = content_hash(data)

    def anchor(item):
        return source_anchor(item['source_ids'], pending, data['source_kind'])

    facts = []
    for item in sorted(result['facts'], key=lambda item: anchor(item)['sequence']):
        source_ids = item['source_ids']
        labels = item['categories']
        facts.append(Fact(
            subject=item['subject'], predicate=item['predicate'], object=item['object'],
            text=item['text'], user_id=user_id,
            valid_at=fact_valid_at(item, anchor(item)['event_time']), created_at=now, last_access=now,
            categories=list(labels), categories_source='llm' if labels else 'unset',
            object_kind=item['object_kind'], sensitive=item['sensitive'],
            provenance=list(source_ids),
            source_roles=list(dict.fromkeys(views[key]['role'] for key in source_ids if views[key]['role'])),
            cardinality=item['cardinality'], retention=item['retention'],
            id='ft_' + content_hash([input_hash, item])[:24],
        ))
    return facts


class FactWriter:
    def __init__(self, fact_store, graph, embedder, config):
        self.fact_store, self.config = fact_store, config
        self.graph_builder = GraphBuilder(graph, embedder)

    @staticmethod
    def empty_stats():
        return {"facts_added": 0, "duplicates": 0, "invalidated": 0,
                "model_calls": 0, "windows_completed": 0}

    def _decay_sweep(self) -> None:
        live = [f for f in self.fact_store.values() if f.is_live() and f.valid_at is not None]
        if not live:
            return
        now_t = max(f.valid_at for f in live)  # 已知的最晚世界时间 = 该 store 的"现在"
        for f in live:
            if f.retention == "durable" or f.access_count > 0:
                continue  # 用户是谁/喜欢什么，以及已被使用过的内容，不淡出
            days = max(0.0, (now_t - f.valid_at) / DAY)
            f.salience = max(0.3, math.exp(-self.config.salience_decay_per_day * days))
            self.fact_store.upsert(f.id, f.embedding, f)
