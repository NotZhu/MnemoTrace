"""Assemble query results from authorized, request-local Postgres reads."""
from dataclasses import dataclass, replace

from ..store.contracts import FactQuery, RetrievalQuery
from ..store.postgres.graph_reads import RetrievalGraph
from ..store.postgres.records import SOURCE, domain
from ..store.postgres.retrieval import Corpus
from ..util import fmt_date
from .answers import SearchResult, digest_match, should_abstain, slot_result
from .context import ContextSection, pack_sections, render_sections
from .events import EventEvidence
from .fusion import rank_facts, weighted_rrf
from .hybrid import order_positive


@dataclass(frozen=True)
class DigestSearchResult:
    """来源摘要检索结果，包含已选证据和覆盖限制。"""
    digests: tuple[EventEvidence, ...]
    limitations: tuple[str, ...]

    @property
    def complete(self):
        """判断摘要检索是否没有已报告的覆盖限制。"""
        return not self.limitations


@dataclass(frozen=True)
class ContextResult:
    """预算内的记忆上下文及各空间的检索覆盖情况。"""
    context: str
    coverage_by_space: dict[str, tuple[str, ...]]
    fact_ids: tuple[str, ...] = ()
    full_tokens: int | None = None
    sections: tuple[ContextSection, ...] = ()

    @property
    def limitations(self):
        """按首次出现顺序汇总各空间的覆盖限制。"""
        return tuple(dict.fromkeys(flag for flags in self.coverage_by_space.values() for flag in flags))

    @property
    def complete(self):
        """判断上下文是否没有已报告的覆盖限制。"""
        return not self.limitations


def retrieval_query(text, embedder, model, config, *, top_k, as_of=None, facts=None):
    """校验检索范围，并按需生成查询向量。

    Args:
        text: 查询文本。
        embedder: 可选的向量编码器。
        model: 向量模型标识。
        config: 提供候选数量上限的配置。
        top_k: 期望结果数量。
        as_of: 可选的事实有效时间。
        facts: 可选的事实筛选条件，与 as_of 互斥。

    Returns:
        带检索范围和可选查询向量的 RetrievalQuery。

    Raises:
        ValueError: 查询、数量、筛选条件或向量维度无效。
    """
    if not isinstance(text, str) or '\x00' in text:
        raise ValueError('Expected query text')
    if type(top_k) is not int or not 1 <= top_k <= 1000:
        raise ValueError('Expected a retrieval count from 1 to 1000')
    if facts is not None and not isinstance(facts, FactQuery):
        raise ValueError('Expected structured fact conditions')
    if facts is not None and as_of is not None:
        raise ValueError('Use either as_of or explicit fact conditions')
    scope = facts if facts is not None else FactQuery(
        valid_at=as_of, limit=max(top_k, config.postgres_candidate_limit),
    )
    if scope.after_id is not None:
        raise ValueError('Retrieval does not accept a browsing cursor')
    if embedder is None:
        return RetrievalQuery(scope, text)
    vector = tuple(embedder.embed(text))
    if len(vector) != embedder.dim:
        raise ValueError('Embedding dimensions differ from the configured model')
    return RetrievalQuery(scope, text, vector, model)


def _score(candidates, config, query):
    """融合数据库候选记录中的排序信号。

    Args:
        candidates: 带各路分数的事实候选。
        config: 排序权重配置。
        query: 当前检索请求。

    Returns:
        排序后的事实及融合分数，以及候选语义分数映射。
    """
    by_id = {item.record.ref.record_id: item for item in candidates}
    semantic = {key: item.semantic for key, item in by_id.items()}
    ranked, _ = rank_facts(
        query.text, (domain(item.record) for item in by_id.values()), config,
        semantic=semantic,
        lexical={key: item.lexical for key, item in by_id.items()},
        graph={key: item.graph for key, item in by_id.items()},
        recency={key: item.recency for key, item in by_id.items()},
        salience={key: item.salience for key, item in by_id.items()},
    )
    return ranked, semantic


def digest_search(view, query, k, *, start=None, end=None):
    """融合词法与向量信号，检索来源摘要。

    Args:
        view: 当前空间的读取快照。
        query: 检索请求。
        k: 返回摘要数量上限。
        start: 可选的事件时间下界，包含。
        end: 可选的事件时间上界，不包含。

    Returns:
        排序后的摘要证据和覆盖限制。
    """
    corpus = Corpus(view, query, SOURCE, start=start, end=end)
    vectors, lexical = corpus.vector_scores(), corpus.lexical_scores()
    ranked = weighted_rrf({'sem': order_positive(vectors), 'lex': order_positive(lexical)}, {})
    ordered = sorted(ranked, key=lambda key: (-ranked[key], key))
    sources = view.digests(ordered[:k])
    evidence = tuple(sources[key] for key in ordered[:k] if key in sources)
    return DigestSearchResult(evidence, corpus.limitations())


def _source_references(view, scope, facts):
    """读取所选事实可见的来源引用。

    Args:
        view: 当前空间的读取快照。
        scope: 事实筛选和脱敏条件。
        facts: 已选事实。

    Returns:
        去重后的来源记录 ID。脱敏时返回空元组。
    """
    if scope.exclude_sensitive:
        return ()
    ids = tuple(dict.fromkeys(key for fact in facts for key in fact.provenance
                              if not scope.source_ids or key in scope.source_ids))
    return view.visible_source_ids(ids)


def search(view, query, config, top_k):
    """检索事实证据，并在允许时用来源摘要补充。

    Args:
        view: 当前空间的读取快照。
        query: 检索请求及事实筛选条件。
        config: 排序与回退配置。
        top_k: 返回结果数量上限。

    Returns:
        候选证据、显式主体和谓词对应的唯一值，或无证据状态。
    """
    scope = query.facts
    if scope.subject is not None and scope.predicate is not None:
        records = view.slot_facts(scope)
        result = slot_result(query.text, [domain(record) for record in records])
        facts = result.facts[:top_k]
        return replace(result, facts=facts, source_ids=_source_references(view, scope, facts))
    graph = RetrievalGraph(view, query)
    batch = view.candidates(query, config=config, graph=graph)
    ranked, sem = _score(batch.candidates, config, query)
    chosen = ranked[:top_k]
    facts = [fact for fact, _ in chosen]
    flags = batch.limitations
    if facts and not should_abstain(query.text, facts, sem.values(), config.abstain_threshold):
        return SearchResult(query.text, facts, [score for _, score in chosen],
                            limitations=flags, source_ids=_source_references(view, scope, facts))
    if config.digest_fallback and not (scope.exclude_sensitive or scope.subject or scope.predicate
                                       or scope.categories or scope.source_ids):
        digests = digest_search(view, query, config.digest_fallback_k)
        flags = tuple(dict.fromkeys((*flags, *digests.limitations)))
        matches = tuple(ep for ep in digests.digests if digest_match(query.text, ep.text))
        if matches:
            return SearchResult(query.text, via='digest', sources=matches[:top_k], limitations=flags)
    return SearchResult(query.text, via='abstain', abstained=True, limitations=flags)


def context(view, query, config, *, n_facts, n_digests, char_budget):
    """在字符预算内组装当前空间的事实和来源摘要。

    Args:
        view: 当前空间的读取快照。
        query: 检索请求及事实筛选条件。
        config: 排序配置。
        n_facts: 事实数量上限。
        n_digests: 来源摘要数量上限。
        char_budget: 上下文字符预算。

    Returns:
        上下文、分段、实际纳入的事实 ID 和覆盖限制。
    """
    graph = RetrievalGraph(view, query)
    batch = view.candidates(query, config=config, graph=graph)
    ranked, _ = _score(batch.candidates, config, query)
    facts = [fact for fact, _ in ranked[:n_facts]]
    scope = query.facts
    fact_lines = [f'- [{fmt_date(fact.valid_at)}] {fact.text}' for fact in facts]
    groups = [('facts', 'FACTS', fact_lines)]
    flags = [*batch.limitations, *graph.limitations()]
    if n_digests and not (scope.exclude_sensitive or scope.subject or scope.predicate
                          or scope.categories or scope.source_ids):
        digests = digest_search(view, query, n_digests)
        flags.extend(digests.limitations)
        if digests.digests:
            groups.append(('digests', 'SOURCE DIGESTS', [
                f'- [{fmt_date(ep.event_time)}] (episode: {ep.id}; session: {ep.session_id}; role={ep.role}) {ep.text}'
                for ep in digests.digests]))
    space_id = view.snapshot.context.space_id
    sections, limited = pack_sections(groups, char_budget, space_id)
    if limited:
        flags.append('context_budget')
    # Only facts included in the delivered context count as retrieval hits.
    remaining = char_budget
    included = []
    for fact, line in zip(facts, fact_lines):
        cost = len(line) + 1 + (0 if included else len('FACTS'))
        if cost <= remaining:
            remaining -= cost
            included.append(fact.id)
    return ContextResult(render_sections(sections), {space_id: tuple(dict.fromkeys(flags))},
                         tuple(included), sections=sections)


def aggregate_facts(view, scope, operation, start, end, evidence_query):
    """统计快照中的匹配事实，并按需查找摘要候选。

    Args:
        view: 当前空间的读取快照。
        scope: 事实筛选条件。
        operation: 统计事实条数或不同对象值的数量。
        start: 有效时间下界，包含。
        end: 有效时间上界，不包含。
        evidence_query: 无匹配时使用的可选摘要检索请求。

    Returns:
        计数、事实证据和可选的摘要候选。
    """
    from .structured import summarize_matches

    def rows():
        cursor = None
        while True:
            batch = view.aggregation_facts(scope, start, end, after_id=cursor)
            yield from (domain(record) for record in batch)
            if len(batch) < 1000:
                return
            cursor = batch[-1].ref.record_id

    result = summarize_matches(rows(), operation, scope.limit)
    if not result.count and evidence_query is not None and not scope.exclude_sensitive:
        sources = digest_search(view, evidence_query, scope.limit, start=start, end=end)
        flags = ('digest_candidates_only', *sources.limitations) if sources.digests else sources.limitations
        return replace(result, candidates=sources.digests, limitations=flags)
    return result


def follow_path(view, scope, anchor, predicates):
    """在同一快照中按谓词顺序扩展匹配路径。

    Args:
        view: 当前空间的读取快照。
        scope: 关联事实的可见性条件与路径上限。
        anchor: 起始实体名称。
        predicates: 规范化的谓词序列。

    Returns:
        路径终点、支撑事实和覆盖限制。
    """
    from .structured import PathMatch, PathResult, traverse

    graph = RetrievalGraph(view, RetrievalQuery(scope, ''))
    entity = graph.get_entity(view.snapshot.context.user_id, anchor)
    if entity is None:
        return PathResult(())
    graph.truncated = False  # Only the explicit traversal contributes to path coverage.

    def neighbors(entity, predicate):
        edges = graph._edges((entity,), 'out', scope.limit + 1, (predicate,))
        facts = {record.ref.record_id: domain(record)
                 for record in view.facts_by_ids(tuple(edge.fact_id for edge in edges), scope)}
        for edge in edges:
            if edge.fact_id in facts:
                yield edge.object_id, facts[edge.fact_id]

    paths, flags = traverse(entity.id, predicates, neighbors, scope.limit)
    return PathResult(tuple(PathMatch(graph.entities[key].name, facts) for key, facts in paths),
                      tuple(dict.fromkeys((*flags, *graph.limitations()))))
