"""Apply normalized facts and their projections; never call a model or schedule work."""
from __future__ import annotations
import math
from ..util import DAY
from ..types import Fact
from .pipeline import content_hash
from .planning import fact_valid_at, source_anchor
from .graph_builder import GraphBuilder


def extraction_entries(data, result, input_hash):
    """为抽取结果分配稳定的事实 ID，并按原文顺序排列。

    Args:
        data: 批次抽取输入。
        result: 已验证的抽取结果。
        input_hash: 批次输入哈希。

    Returns:
        事实 ID 与候选事实组成的列表。
    """
    pending = {item['id']: item for item in data['pending']}
    items = sorted(result['facts'], key=lambda item: source_anchor(
        item['source_ids'], pending)['sequence'])
    entries = [('ft_' + content_hash([input_hash, item])[:24], item) for item in items]
    return list(dict(entries).items()) if 'entity_types' in data else entries


def materialize_facts(user_id, data, now, entries, *, bindings=None):
    """从抽取结果构造带来源和时间信息的事实记录。

    Args:
        user_id: 事实所属的用户 ID。
        data: 批次抽取输入。
        now: 事实记录时间戳。
        entries: 候选事实及其稳定 ID。
        bindings: 可选的本体绑定，提供时只构造已绑定记录。

    Returns:
        待合并的 Fact 列表。
    """
    pending = {item['id']: item for item in data['pending']}

    def anchor(item):
        return source_anchor(item['source_ids'], pending)

    facts = []
    for key, item in entries:
        if bindings is not None and key not in bindings:
            continue
        source_ids = item['source_ids']
        labels = item['categories']
        binding = bindings[key] if bindings is not None else {'predicate': item['predicate']}
        fields = {'subject': item['subject'], 'object': item['object']} | binding
        facts.append(Fact(
            **fields,
            text=item['text'], user_id=user_id,
            valid_at=fact_valid_at(item, anchor(item)['event_time']), created_at=now, last_access=now,
            categories=list(labels),
            sensitive=item['sensitive'],
            provenance=list(source_ids),
            retention=item['retention'],
            id=key,
        ))
    return facts


class FactWriter:
    def __init__(self, fact_store, graph, config):
        self.fact_store, self.config = fact_store, config
        self.graph_builder = GraphBuilder(graph)

    @staticmethod
    def empty_stats():
        """创建计数均为零的事实整合统计。"""
        return {"facts_added": 0, "duplicates": 0, "invalidated": 0,
                "model_calls": 0, "windows_completed": 0}

    def _decay_sweep(self) -> None:
        """按最新有效时间更新未访问且非持久的热层事实显著度。"""
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
