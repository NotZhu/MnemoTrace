"""混合检索(AGENTS.md 「检索与模块」):在 live fact 集合上融合 dense-semantic + BM25-lexical + graph-proximity +
新近度 + 显著度,并结合加权的 Reciprocal Rank Fusion。"""
from __future__ import annotations
from ..util import time_key

import re
from typing import Optional

from ..config import Config
from ..focus import TRACK_BOOST, matches as focus_matches
from ..consolidate.graph_builder import projects_fact
from ..embed import Embedder
from ..store import GraphStore, VectorStore
from ..types import Fact
from ..util import cosine, fmt_date, now, recency
from .fusion import rank_facts
from .lexical import bm25_scores, lexical_terms, normalize, overlap_terms

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_GRAPH_HOP_DECAY = 0.65


def date_terms(epoch: float) -> str:
    """生成可参与词法匹配的日期词项。

    Args:
        epoch: 事实或事件的时间戳。

    Returns:
        日期、年份、数字月份和英文月份名。无法解析时返回空字符串。
    """
    try:
        d = fmt_date(epoch)  # YYYY-MM-DD
        y, m, _ = d.split("-")
        return f"{d} {y} {m} {_MONTHS[int(m) - 1]}"
    except Exception:  # noqa: BLE001
        return ""

def order_positive(scores: dict[str, float]) -> list[str]:
    """按分数降序保留有正向证据的候选。

    Args:
        scores: 候选 ID 到分数的映射。

    Returns:
        正分候选 ID，供排名融合使用。
    """
    return [doc_id for doc_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True) if score > 0.0]


def graph_relation_relevance(query: str, fact: Fact) -> float:
    """按查询与事实谓词或文本的词项重合程度打分。

    Args:
        query: 查询文本。
        fact: 待打分的事实。

    Returns:
        谓词匹配为 1，只有文本匹配为 0.75，无匹配为 0。
    """
    if set(lexical_terms(query, query=True)) & set(lexical_terms(fact.predicate)):
        return 1.0
    return 0.75 if overlap_terms(query, fact.text) else 0.0


def _entity_name_mentions(text: str, name: str) -> list[re.Match]:
    """查找规范化文本中的实体名称。

    Args:
        text: 已规范化的待匹配文本。
        name: 实体名称。

    Returns:
        匹配的位置。ASCII 名称要求字母数字边界。
    """
    if not name.strip():
        return []
    name = normalize(name)
    escaped = re.escape(name)
    if name.isascii():
        pattern = re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.IGNORECASE)
    else:
        pattern = re.compile(escaped, re.IGNORECASE)
    return list(pattern.finditer(text))


def mentioned_entities(query, entities):
    """从查询中找出无歧义的实体名称，重叠时优先匹配长名称。

    Args:
        query: 查询文本。
        entities: 可用于匹配的实体。

    Returns:
        按首次匹配位置排列的实体 ID 到实体的映射。
    """
    query = normalize(query)
    mentions = {}
    for entity in entities:
        if entity.name == "我" and query != "我":
            continue
        for match in _entity_name_mentions(query, entity.name):
            mentions.setdefault(match.span(), {})[entity.id] = entity
    selected = []
    occupied = set()
    for (start, end), matches in sorted(mentions.items(), key=lambda item: (
        item[0][0] - item[0][1], item[0][0],
    )):
        positions = set(range(start, end))
        if positions & occupied:
            continue
        occupied.update(positions)
        if len(matches) == 1:
            entity_id, entity = next(iter(matches.items()))
            selected.append((start, entity_id, entity))
    return {entity_id: entity for _, entity_id, entity in sorted(selected, key=lambda item: item[0])}


class HybridRetriever:
    """使用语义、词法、图、新近度和显著度融合排序事实。"""
    def __init__(self, fact_store: VectorStore, graph: GraphStore, embedder: Embedder | None, config: Config) -> None:
        """绑定事实、图和排序依赖。

        Args:
            fact_store: 事实存储。
            graph: 实体和关系存储。
            embedder: 可选的向量编码器。
            config: 候选召回、图遍历和排序配置。
        """
        self.track = ()
        self.fact_store = fact_store
        self.graph = graph
        self.embedder = embedder
        self.config = config

    def query_entity_ids(self, query: str, user_id: str) -> set[str]:
        """识别查询文本中的锚点实体。

        Args:
            query: 查询文本。
            user_id: 实体所属的用户 ID。

        Returns:
            查询中无歧义的实体 ID 集合。
        """
        entities = [ent for ent in self.graph.entities.values() if ent.user_id == user_id]
        return set(mentioned_entities(query, entities))

    def _graph_scores(
        self, query: str, user_id: str, live: list[Fact], as_of: Optional[float]
    ) -> tuple[dict[str, float], set[str]]:
        """从锚点实体传播分数，计算候选事实的图相关性。

        Args:
            query: 查询文本。
            user_id: 实体所属的用户 ID。
            live: 可参与计分的事实。
            as_of: 图遍历使用的有效时间。

        Returns:
            事实 ID 到图分数的映射，以及查询锚点 ID。
        """
        qids = self.query_entity_ids(query, user_id)
        if not self.config.graph_proximity:
            return {f.id: 0.0 for f in live}, qids
        live_fact_ids = {f.id for f in live}
        node_scores: dict[str, float] = {eid: 1.0 for eid in qids}
        frontier: dict[str, float] = {eid: 1.0 for eid in qids}

        for depth in range(1, max(0, self.config.max_hops) + 1):
            next_frontier: dict[str, float] = {}
            for eid, parent_score in frontier.items():
                for direction in ("out", "in"):
                    for rel in self.graph.neighbors(eid, as_of, direction):
                        if rel.fact_id not in live_fact_ids:
                            continue
                        neighbor_id = rel.object_id if direction == "out" else rel.subject_id
                        if neighbor_id in qids:
                            continue
                        contribution = parent_score * _GRAPH_HOP_DECAY
                        if self.config.graph_path_reinforcement:
                            old_score = node_scores.get(neighbor_id, 0.0)
                            node_scores[neighbor_id] = min(1.0, old_score + contribution)
                            next_frontier[neighbor_id] = next_frontier.get(neighbor_id, 0.0) + contribution
                        else:
                            old_score = node_scores.get(neighbor_id, 0.0)
                            if old_score >= contribution:
                                continue
                            node_scores[neighbor_id] = contribution
                            next_frontier[neighbor_id] = max(next_frontier.get(neighbor_id, 0.0), contribution)
            if not next_frontier:
                break
            frontier = {eid: min(score, 1.0) for eid, score in next_frontier.items()}

        scores: dict[str, float] = {}
        query_terms = set(lexical_terms(query, query=True))
        use_relation_weight = self.config.graph_relation_awareness and any(
            query_terms.intersection(lexical_terms(fact.predicate)) for fact in live
        )
        for f in live:
            if not projects_fact(f) or not node_scores:
                scores[f.id] = 0.0
                continue
            subj = self.graph.get_entity(f.user_id, f.subject)
            obj = self.graph.get_entity(f.user_id, f.object)
            sid = subj.id if subj else None
            oid = obj.id if obj else None
            score = max(node_scores.get(sid, 0.0), node_scores.get(oid, 0.0))
            if use_relation_weight and score > 0.0:
                score *= 0.55 + 0.45 * graph_relation_relevance(query, f)
            scores[f.id] = score
        return scores, qids

    # --- 候选生成(可选的规模路径;config.ann_candidates) ----------------------------------------------
    def _ann_candidates(
        self, query: str, user_id: str, as_of: Optional[float], qvec: list[float]
    ) -> list[Fact]:
        """合并向量候选与查询锚点的一跳事实。

        Args:
            query: 查询文本。
            user_id: 事实所属的用户 ID。
            as_of: 可选的事实有效时间。
            qvec: 查询向量。

        Returns:
            去重后的候选事实。
        """
        pool = max(self.config.ann_pool, self.config.top_k)
        hits = self.fact_store.search(
            qvec, pool, where=lambda f: f.user_id == user_id and f.is_live(as_of)
        )
        live = [f for _, f in hits]
        seen = {f.id for f in live}
        for f in self._graph_neighbor_facts(query, user_id, as_of):
            if f.id not in seen:
                live.append(f)
                seen.add(f.id)
        return live

    def _graph_neighbor_facts(self, query: str, user_id: str, as_of: Optional[float]) -> list[Fact]:
        """读取查询锚点双向一跳关系对应的有效事实。

        Args:
            query: 查询文本。
            user_id: 锚点实体所属的用户 ID。
            as_of: 可选的事实有效时间。

        Returns:
            从事实存储读取的邻接事实。
        """
        out: list[Fact] = []
        for eid in self.query_entity_ids(query, user_id):
            for direction in ("out", "in"):
                for rel in self.graph.neighbors(eid, as_of, direction):
                    f = self.fact_store.get(rel.fact_id)
                    if f is not None and f.is_live(as_of):
                        out.append(f)
        return out

    def retrieve(
        self, query: str, user_id: str, as_of: Optional[float] = None, top_k: Optional[int] = None, *, candidates=None
    ) -> tuple[list[tuple[Fact, float]], dict]:
        """生成候选并融合各路信号对事实排序。

        Args:
            query: 查询文本。
            user_id: 事实所属的用户 ID。
            as_of: 可选的事实有效时间。
            top_k: 返回数量上限。
            candidates: 可选的预筛选候选事实。

        Returns:
            排序后的事实与分数，以及各路信号诊断信息。
        """
        top_k = top_k or self.config.top_k
        qvec = self.embedder.embed(query) if self.embedder is not None else None
        # 候选集。默认:扫描该用户全部 live fact(精确,几千条以内没问题)。可选(config.ann_candidates):
        # 来自向量索引的有界 ANN 池 ∪ 查询的图邻居 fact。BM25 只对候选事实打分。下游一切
        # (5 信号融合)完全相同;改变的只是哪些 fact 参与打分。
        # 当池覆盖全部 fact 时两者等价。
        if candidates is not None:
            live = list(candidates)
        elif self.config.ann_candidates and qvec is not None:
            live = self._ann_candidates(query, user_id, as_of, qvec)
        else:
            live = [f for f in self.fact_store.values() if f.user_id == user_id and f.is_live(as_of)]
        if not live:
            return [], {"sem": {}, "lex": {}, "qids": set()}

        sem = {f.id: cosine(qvec, f.embedding or []) for f in live} if qvec is not None else {}
        # 把每个 fact 的日期变成可检索 token,使提到时间的查询('May 2023')能按日期命中
        lex = bm25_scores(query, [(f.id, f"{f.text} {date_terms(f.valid_at)}") for f in live])
        gph, qids = self._graph_scores(query, user_id, live, as_of)
        t = now() if as_of is None else as_of
        rec = {f.id: recency(max(0.0, t - time_key(f.valid_at)), self.config.recency_tau_days) for f in live}
        sal = {f.id: f.salience + (TRACK_BOOST if focus_matches(f.text, self.track, f.categories) else 0)
               for f in live}

        ranked, fused = rank_facts(query, live, self.config, semantic=sem, lexical=lex, graph=gph,
                                  recency=rec, salience=sal)
        diag = {"sem": sem, "lex": lex, "fused": fused, "qids": qids}
        return ranked[:top_k], diag
