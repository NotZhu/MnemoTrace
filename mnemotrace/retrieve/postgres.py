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
from .fusion import order_by_score, weighted_rrf
from .hybrid import order_positive
from .lexical import literal_value_match


@dataclass(frozen=True)
class DigestSearchResult:
    digests: tuple[EventEvidence, ...]
    limitations: tuple[str, ...]

    @property
    def complete(self):
        return not self.limitations


@dataclass(frozen=True)
class ContextResult:
    context: str
    coverage_by_space: dict[str, tuple[str, ...]]
    fact_ids: tuple[str, ...] = ()
    full_tokens: int | None = None
    sections: tuple[ContextSection, ...] = ()

    @property
    def limitations(self):
        return tuple(dict.fromkeys(flag for flags in self.coverage_by_space.values() for flag in flags))

    @property
    def complete(self):
        return not self.limitations


def retrieval_query(text, embedder, model, config, *, top_k, as_of=None, facts=None):
    if not isinstance(text, str) or '\x00' in text:
        raise ValueError('Expected query text')
    if type(top_k) is not int or not 1 <= top_k <= 1000:
        raise ValueError('Expected a retrieval count from 1 to 1000')
    if facts is not None and not isinstance(facts, FactQuery):
        raise ValueError('Expected structured fact conditions')
    if facts is not None and as_of is not None:
        raise ValueError('Use either as_of or explicit fact conditions')
    scope = facts if facts is not None else FactQuery(
        valid_at=as_of, limit=max(top_k, config.ann_pool),
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
    sem = {item.record.ref.record_id: item.semantic for item in candidates}
    records = {item.record.ref.record_id: item.record for item in candidates}
    facts = {key: domain(record) for key, record in records.items()}
    rankings = {'sem': order_positive(sem)}
    for name, key, order in (('lex', 'lexical', order_positive), ('graph', 'graph', order_positive),
                             ('rec', 'recency', order_by_score), ('sal', 'salience', order_by_score)):
        rankings[name] = order({item.record.ref.record_id: getattr(item, key) for item in candidates})
    weights = {name: getattr(config, f'w_{name}') for name in rankings}
    scores = weighted_rrf(rankings, weights, config.rrf_k)
    ids = sorted(scores, key=lambda key: (not literal_value_match(query.text, facts[key].object), -scores[key], key))
    return [(facts[key], scores[key]) for key in ids], sem


def digest_search(view, query, k, *, start=None, end=None):
    corpus = Corpus(view, query, SOURCE, start=start, end=end)
    vectors, lexical = corpus.vector_scores(), corpus.lexical_scores()
    ranked = weighted_rrf({'sem': order_positive(vectors), 'lex': order_positive(lexical)}, {})
    ordered = sorted(ranked, key=lambda key: (-ranked[key], key))
    sources = view.digests(ordered[:k])
    evidence = tuple(sources[key] for key in ordered[:k] if key in sources)
    return DigestSearchResult(evidence, corpus.limitations())


def _source_references(view, scope, facts):
    if scope.exclude_sensitive:
        return ()
    ids = tuple(dict.fromkeys(key for fact in facts for key in fact.provenance
                              if not scope.source_ids or key in scope.source_ids))
    return view.visible_source_ids(ids)


def search(view, query, config, top_k):
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
