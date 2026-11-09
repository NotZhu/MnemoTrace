"""混合检索(AGENTS.md 「检索与模块」):在 live fact 集合上融合 dense-semantic + BM25-lexical + graph-proximity +
新近度 + 显著度,并结合加权的 Reciprocal Rank Fusion。"""
from __future__ import annotations
from ..util import time_key

import re
from typing import Optional

from ..config import Config
from ..embed import Embedder
from ..store import GraphStore, VectorStore
from ..types import Fact
from ..util import cosine, fmt_date, now, recency, tokenize
from .fusion import order_by_score, weighted_rrf
from .lexical import bm25_scores, lexical_terms, normalize, stem, stems

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_GRAPH_HOP_DECAY = 0.65


def date_terms(epoch: float) -> str:
    """把 fact 的日期渲染成可检索 token(年、数字月份、月份名),使提到时间的查询('May 2023'、'in 2024')
    能通过 BM25 命中日期正确的 fact —— 否则日期只存在于 valid_at 中,对检索不可见。这是把查询期的
    时间匹配当作词法信号来实现(精神上类似 MemoryScope 的 time_ratio),不引入可能压过相关性的
    分数乘子。"""
    try:
        d = fmt_date(epoch)  # YYYY-MM-DD
        y, m, _ = d.split("-")
        return f"{d} {y} {m} {_MONTHS[int(m) - 1]}"
    except Exception:  # noqa: BLE001
        return ""

# 标记持久身份/偏好类 fact(区别于偶然事件提及)的谓词。用于按类型加权融合——
# 这些 fact 会获得检索加权(AGENTS.md 「检索与模块」;MemoryScope/OMEGA 的收敛结论)。
_PREFERENCE_PREDS = frozenset({
    "likes", "dislikes", "prefers", "avoids", "loves", "hates", "enjoys", "wants",
    "allergic_to", "interested_in", "favorite", "prefers_to",
})
_IDENTITY_PREDS = frozenset({
    "name", "works_at", "lives_in", "born_in", "married_to", "occupation", "age",
    "studied_at", "owns", "has", "speaks",
})
_GRAPH_PREDICATE_ALIASES: dict[str, tuple[str, ...]] = {
    "works_at": ("work", "works", "working", "employer", "employed", "employment", "company", "job"),
    "occupation": ("profession", "role", "title", "job", "career"),
    "lives_in": ("live", "lives", "living", "reside", "resides", "city", "home", "location", "where"),
    "based_in": ("based", "base", "located", "location", "headquarter", "headquarters", "hq", "city", "where"),
    "located_in": ("located", "location", "based", "headquarter", "headquarters", "hq", "city", "where"),
    "works_on": ("project", "work", "works", "working", "built", "building"),
    "project": ("project", "initiative", "work"),
    "colleague": ("colleague", "coworker", "co-worker", "workmate"),
    "sister": ("sister", "sibling"),
    "brother": ("brother", "sibling"),
    "spouse": ("spouse", "wife", "husband", "partner"),
    "wife": ("spouse", "wife", "partner"),
    "husband": ("spouse", "husband", "partner"),
    "manager": ("manager", "boss", "lead"),
    "likes": ("like", "likes", "favorite", "interest"),
    "favorite": ("favorite", "favourite", "likes", "prefers"),
}
_GRAPH_QUERY_CUE_WORDS = frozenset(
    word
    for words in _GRAPH_PREDICATE_ALIASES.values()
    for alias in words
    for word in stems(alias)
) | frozenset({"where", "who", "whose", "which"})
_SELF_ANCHOR_RE = re.compile(
    r"\b(i|me|my|mine|myself|we|our|ours|user's|the user's|their|his|her)\b|我|我的|用户|用户的",
    re.IGNORECASE,
)
_ENTITY_ANCHOR_STOP = frozenset({
    "the", "and", "for", "with", "from", "inc", "ltd", "llc", "corp", "co", "company", "project",
    "user", "assistant", "team", "group", "system", "ai",
})
_EXCLUSION_BEFORE_RE = re.compile(
    r"(?:\bnot\b(?:\s+\w+){0,5}|\bexcept\b(?:\s+\w+){0,4}|\bexcluding\b(?:\s+\w+){0,4}|"
    r"\bexclude\b(?:\s+\w+){0,4}|\bother\s+than\b(?:\s+\w+){0,4}|"
    r"\brather\s+than\b(?:\s+\w+){0,4}|\bbesides\b(?:\s+\w+){0,4}|不是|不在|排除|除了)\s*$",
    re.IGNORECASE,
)
_EXCLUSION_VALUE_PREDS = frozenset({
    "based_in", "located_in", "headquartered_in", "lives_in", "works_at", "occupation",
    "likes", "dislikes", "prefers", "favorite", "owns", "has", "diet", "allergic_to",
})


def order_positive(scores: dict[str, float]) -> list[str]:
    """只对真实证据命中排序。

    RRF 会给每个上榜项正贡献。对证据类信号(semantic、lexical、graph)而言,分数为 0 表示"该信号
    没找到证据",而不是"排在最后的证据"。过滤掉 0 分,可以防止 salience/recency 这类先验借助任意的
    0 分排序冒领虚假支持。
    """
    return [doc_id for doc_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True) if score > 0.0]


def fact_type_weight(fact: Fact, config: Config) -> float:
    """按 fact 类型的检索乘子:preference > identity > 偶发提及。当两者都匹配查询时,持久的
    "他们是谁 / 喜欢什么"类 fact 应排在一次性提及之前。"""
    p = fact.predicate.lower()
    if p in _PREFERENCE_PREDS or p.startswith("favorite"):
        return config.w_type_preference
    if p in _IDENTITY_PREDS:
        return config.w_type_identity
    return 1.0


def _graph_predicate_terms(predicate: str) -> set[str]:
    pred = predicate.lower()
    terms = set(stems(pred.replace("_", " ")))
    for alias in _GRAPH_PREDICATE_ALIASES.get(pred, ()):
        terms.update(stems(alias))
    return terms


def graph_relation_relevance(query: str, fact: Fact) -> float:
    """该 fact 的关系与查询关系意图的匹配程度。

    graph proximity 找到的是*邻近*的 fact;这个分数让它以查询为条件,使公司/地点类问题优先选择
    works_at/based_in 边,而不是同一节点的 likes/owns 之类干扰项。若查询不含关系线索
    ("tell me about Wei"),调用方应完全跳过关系加权。
    """
    q_terms = set(lexical_terms(query, query=True))
    pred_terms = _graph_predicate_terms(fact.predicate)
    if q_terms & pred_terms:
        return 1.0
    text_terms = set(lexical_terms(f"{fact.subject} {fact.object} {fact.text}"))
    if q_terms & text_terms:
        return 0.75
    return 0.0


def _entity_anchor_terms(name: str, aliases: list[str]) -> set[str]:
    return {term for text in (name, *aliases) for term in lexical_terms(text, query=True, exact=True)
            if term.isascii() and len(term) >= 3 and not term.isdigit() and stem(term) not in _ENTITY_ANCHOR_STOP}


def _entity_name_mentions(text: str, name: str) -> list[re.Match]:
    if not name.strip() or len(name) > 96:
        return []
    name = normalize(name)
    escaped = re.escape(name)
    if name.isascii():
        pattern = re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.IGNORECASE)
    else:
        pattern = re.compile(escaped, re.IGNORECASE)
    return list(pattern.finditer(text))


def mentioned_entities(query, entities, resolve_alias=None):
    query = normalize(query)
    mentions = {}
    for entity in tuple(entities):
        for name in (entity.name, *entity.aliases):
            matches = _entity_name_mentions(query, name)
            if not matches:
                continue
            target = resolve_alias(name) if resolve_alias is not None and name != entity.name else entity
            for match in matches:
                mentions.setdefault(match.span(), {})[target.id if target is not None else None] = target
    selected = []
    occupied = set()
    for (start, end), matches in sorted(mentions.items(), key=lambda item: (
        item[0][0] - item[0][1], item[0][0],
    )):
        positions = set(range(start, end))
        if positions & occupied:
            continue
        occupied.update(positions)
        if len(matches) == 1 and None not in matches:
            entity_id, entity = next(iter(matches.items()))
            selected.append((start, entity_id, entity))
    return {entity_id: entity for _, entity_id, entity in sorted(selected, key=lambda item: item[0])}


def _is_excluded_mention(query_l: str, name: str) -> bool:
    for match in _entity_name_mentions(query_l, name):
        before = query_l[max(0, match.start() - 72):match.start()]
        if _EXCLUSION_BEFORE_RE.search(before):
            return True
    return False


class HybridRetriever:
    """混合检索器:融合语义、词法、图关系、显著度和新近度的检索。"""
    def __init__(self, fact_store: VectorStore, graph: GraphStore, embedder: Embedder | None, config: Config) -> None:
        """初始化混合检索器。

        Args:
            fact_store: 事实存储。
            graph: 图存储。
            embedder: 嵌入器。
            config: 配置。
        """
        self.fact_store = fact_store
        self.graph = graph
        self.embedder = embedder
        self.config = config
        self._semantic = embedder is not None

    def query_entity_ids(self, query: str, user_id: str) -> set[str]:
        """全名出现在查询中的实体节点(即查询的锚点实体)。"""
        q = set(lexical_terms(query, query=True, exact=True))
        entities = [ent for ent in self.graph.entities.values() if ent.user_id == user_id]
        ids = set(mentioned_entities(query, entities, getattr(self.graph, 'resolve_mention_alias', None)))
        if self.config.graph_entity_alias_anchor and not ids:
            term_to_ids: dict[str, set[str]] = {}
            for ent in entities:
                for term in _entity_anchor_terms(ent.name, []):
                    term_to_ids.setdefault(term, set()).add(ent.id)
            for term in q:
                hits = term_to_ids.get(term)
                if hits is not None and len(hits) == 1:
                    ids.update(hits)
        if self.config.graph_self_anchor and _SELF_ANCHOR_RE.search(query):
            for name in (user_id, "user"):
                ent = self.graph.get_entity(user_id, name)
                if ent is not None:
                    ids.add(ent.id)
        ids -= self.graph_excluded_entity_ids(query, user_id)
        return ids

    def graph_excluded_entity_ids(self, query: str, user_id: str) -> set[str]:
        """被查询显式排除的实体("not in Lisbon"、"except Zephyr"、"不是上海")。

        负向约束在打分前应用,被排除的值不会仅因为查询提到了它而获得加权。这样图扩展对带约束的
        multi-hop 问题保持有用,而不会浮现用户明确要求避开的那条干扰路径。
        """
        if not (self.config.graph_proximity and self.config.graph_negative_constraints):
            return set()
        query_l = normalize(query)
        direct: set[str] = set()
        entities = [ent for ent in self.graph.entities.values() if ent.user_id == user_id]
        for ent in entities:
            for name in (ent.name, *ent.aliases):
                if _is_excluded_mention(query_l, name):
                    direct.add(ent.id)
                    break
        term_to_ids: dict[str, set[str]] = {}
        for ent in entities:
            for term in _entity_anchor_terms(ent.name, ent.aliases):
                term_to_ids.setdefault(term, set()).add(ent.id)
        for term, hits in term_to_ids.items():
            if len(hits) == 1 and _is_excluded_mention(query_l, term):
                direct.update(hits)
        return direct

    def graph_exclusion_zone(self, query: str, user_id: str, as_of: Optional[float]) -> set[str]:
        """直接被排除的实体,加上其属性值被排除的实体。

        例如:查询说 "project not in Lisbon",`Lisbon` 被直接排除,而 `Zephyr --based_in--> Lisbon`
        这条边会把 Zephyr 从候选路径中排除。`Wei --colleague--> Lin` 这类关系边不用于扩展,
        所以 "except Lin" 不会抹掉 Wei。
        """
        direct = self.graph_excluded_entity_ids(query, user_id)
        if not direct:
            return set()
        out = set(direct)
        for rel in self.graph.relations():
            if rel.predicate.lower() not in _EXCLUSION_VALUE_PREDS:
                continue
            live = as_of is None or ((rel.valid_at is not None and rel.valid_at <= as_of) and (rel.invalid_at is None or rel.invalid_at > as_of))
            if live and rel.object_id in direct:
                out.add(rel.subject_id)
        return out

    def _fact_touches_entities(self, fact: Fact, entity_ids: set[str]) -> bool:
        if not entity_ids:
            return False
        subj = self.graph.get_entity(fact.user_id, fact.subject)
        obj = self.graph.get_entity(fact.user_id, fact.object)
        return (subj is not None and subj.id in entity_ids) or (obj is not None and obj.id in entity_ids)

    def _graph_scores(
        self, query: str, user_id: str, live: list[Fact], as_of: Optional[float]
    ) -> tuple[dict[str, float], set[str]]:
        qids = self.query_entity_ids(query, user_id)
        if not self.config.graph_proximity:
            return {f.id: 0.0 for f in live}, qids
        excluded = self.graph_exclusion_zone(query, user_id, as_of)
        qids -= excluded
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
                        if rel.subject_id in excluded or rel.object_id in excluded:
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
        q_terms = set(stems(query)) | {stem(t) for t in tokenize(query)}
        use_relation_weight = self.config.graph_relation_awareness and bool(q_terms & _GRAPH_QUERY_CUE_WORDS)
        for f in live:
            subj = self.graph.get_entity(f.user_id, f.subject)
            obj = self.graph.get_entity(f.user_id, f.object)
            sid = subj.id if subj else None
            oid = obj.id if obj else None
            if (sid is not None and sid in excluded) or (oid is not None and oid in excluded):
                scores[f.id] = 0.0
                continue
            score = max(node_scores.get(sid, 0.0), node_scores.get(oid, 0.0))
            if use_relation_weight and score > 0.0:
                score *= 0.55 + 0.45 * graph_relation_relevance(query, f)
            scores[f.id] = score
        return scores, qids

    # --- 候选生成(可选的规模路径;config.ann_candidates) ----------------------------------------------
    def _ann_candidates(
        self, query: str, user_id: str, as_of: Optional[float], qvec: list[float]
    ) -> list[Fact]:
        """Collect vector and graph candidates without enumerating the fact store."""
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
        """查询锚点实体相关关系(1 跳、双向)所依据的 live fact,按 fact_id 取回——
        因此即使关系命中在向量池之外也能存活。"""
        out: list[Fact] = []
        for eid in self.query_entity_ids(query, user_id):
            for direction in ("out", "in"):
                for rel in self.graph.neighbors(eid, as_of, direction):
                    f = self.fact_store.get(rel.fact_id)
                    if f is not None and f.is_live(as_of):
                        out.append(f)
        return out

    def retrieve(
        self, query: str, user_id: str, as_of: Optional[float] = None, top_k: Optional[int] = None
    ) -> tuple[list[tuple[Fact, float]], dict]:
        top_k = top_k or self.config.top_k
        qvec = self.embedder.embed(query) if self.embedder is not None else None
        # 候选集。默认:扫描该用户全部 live fact(精确,几千条以内没问题)。可选(config.ann_candidates):
        # 来自向量索引的有界 ANN 池 ∪ 查询的图邻居 fact。BM25 只对候选事实打分。下游一切
        # (slot-head 收敛、排除区、5 信号融合)完全相同;改变的只是哪些 fact 参与打分。
        # 当池覆盖全部 fact 时两者等价。
        if self.config.ann_candidates and qvec is not None:
            live = self._ann_candidates(query, user_id, as_of, qvec)
        else:
            live = [f for f in self.fact_store.values() if f.user_id == user_id and f.is_live(as_of)]
        excluded = self.graph_exclusion_zone(query, user_id, as_of)
        if excluded:
            live = [f for f in live if not self._fact_touches_entities(f, excluded)]
        if not live:
            return [], {"sem": {}, "lex": {}, "qids": set()}

        sem = {f.id: cosine(qvec, f.embedding or []) for f in live} if qvec is not None else {}
        # 按类型加权的检索:用 fact 类型缩放 SEMANTIC 分数。因为离题 fact 的 sem≈0,乘子只在真正
        # 相关的候选间重排(preference fact 胜过同等相关的偶发 fact),绝不会把不相关 fact 抬上来。
        if self._semantic:
            for f in live:
                tw = fact_type_weight(f, self.config)
                if tw != 1.0:
                    sem[f.id] *= tw
        # 把每个 fact 的日期变成可检索 token,使提到时间的查询('May 2023')能按日期命中
        lex = bm25_scores(query, [(f.id, f"{f.text} {date_terms(f.valid_at)}") for f in live])
        gph, qids = self._graph_scores(query, user_id, live, as_of)
        t = now() if as_of is None else as_of
        rec = {f.id: recency(max(0.0, t - time_key(f.valid_at)), self.config.recency_tau_days) for f in live}
        sal = {f.id: f.salience for f in live}

        rankings = {
            "sem": order_positive(sem),
            "lex": order_positive(lex),
            "graph": order_positive(gph),
            "rec": order_by_score(rec),
            "sal": order_by_score(sal),
        }
        weights = {
            "sem": self.config.w_sem,
            "lex": self.config.w_lex,
            "graph": self.config.w_graph,
            "rec": self.config.w_rec,
            "sal": self.config.w_sal,
        }
        fused = weighted_rrf(rankings, weights, self.config.rrf_k)
        ranked = sorted(live, key=lambda f: fused.get(f.id, 0.0), reverse=True)[:top_k]
        diag = {"sem": sem, "lex": lex, "fused": fused, "qids": qids}
        return [(f, fused.get(f.id, 0.0)) for f in ranked], diag
