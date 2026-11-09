"""Assemble query results from authorized, request-local Postgres reads."""
from dataclasses import dataclass, replace

from ..store.contracts import FactQuery, RetrievalQuery
from ..store.postgres.graph_reads import RetrievalGraph
from ..store.postgres.records import SOURCE, domain
from ..store.postgres.retrieval import Corpus
from ..util import fmt_date, time_key
from .answers import SearchResult, _GENERIC_ATTR_TERMS, should_abstain
from .events import EventEvidence, event_evidence
from .fusion import order_by_score, weighted_rrf
from .hybrid import fact_type_weight, order_positive
from .lexical import literal_value_match, overlap_terms


@dataclass(frozen=True)
class SourceSearchResult:
    episodes: tuple[EventEvidence, ...]
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
        valid_at=as_of, known_at=as_of, limit=max(top_k, config.ann_pool),
    )
    if scope.after_id is not None:
        raise ValueError('Retrieval does not accept a browsing cursor')
    if embedder is None:
        return RetrievalQuery(scope, text)
    vector = tuple(embedder.embed(text))
    if len(vector) != embedder.dim:
        raise ValueError('Embedding dimensions differ from the configured model')
    return RetrievalQuery(scope, text, vector, model)


def _score(candidates, config, semantic, query):
    sem = {item.record.ref.record_id: item.semantic for item in candidates}
    records = {item.record.ref.record_id: item.record for item in candidates}
    facts = {key: domain(record).as_known_at(query.facts.known_at) for key, record in records.items()}
    if semantic:
        sem = {key: score * fact_type_weight(facts[key], config) for key, score in sem.items()}
    rankings = {'sem': order_positive(sem)}
    for name, key, order in (('lex', 'lexical', order_positive), ('graph', 'graph', order_positive),
                             ('rec', 'recency', order_by_score), ('sal', 'salience', order_by_score)):
        rankings[name] = order({item.record.ref.record_id: getattr(item, key) for item in candidates})
    weights = {name: getattr(config, f'w_{name}') for name in rankings}
    scores = weighted_rrf(rankings, weights, config.rrf_k)
    ids = sorted(scores, key=lambda key: (not literal_value_match(query.text, facts[key].object), -scores[key], key))
    return [(facts[key], scores[key]) for key in ids], sem


def source_search(view, query, k, *, field=None, start=None, end=None):
    corpus = Corpus(view, query, SOURCE, source_field=field, start=start, end=end)
    vectors, lexical = corpus.vector_scores(), corpus.lexical_scores()
    ranked = weighted_rrf({'sem': order_positive(vectors), 'lex': order_positive(lexical)}, {})
    ordered = sorted(ranked, key=lambda key: (-ranked[key], key))
    sources = {item.ref.record_id: domain(item) for item in view.sources(tuple(ordered), known_at=query.facts.known_at)}
    evidence = tuple(event_evidence(sources[key]) for key in ordered[:k])
    return SourceSearchResult(evidence, corpus.limitations())


def search(view, query, config, top_k, semantic):
    graph = RetrievalGraph(view, query)
    batch = view.candidates(query, config=config, graph=graph)
    ranked, sem = _score(batch.candidates, config, semantic, query)
    chosen = ranked[:top_k]
    facts = [fact for fact, _ in chosen]
    flags = batch.limitations
    abstained = not facts or should_abstain(query.text, facts, sem.values(), config.abstain_threshold)
    if abstained and config.digest_fallback and not query.facts.exclude_sensitive:
        sources = source_search(view, query, config.digest_fallback_k, field='digest')
        flags = tuple(dict.fromkeys((*flags, *sources.limitations)))
        matched = [(len(overlap_terms(query.text, ep.text) - _GENERIC_ATTR_TERMS), ep) for ep in sources.episodes]
        if matched:
            hits, best = max(matched, key=lambda row: (row[0], time_key(row[1].event_time), row[1].id))
            if hits > 0:
                answer = f'[{fmt_date(best.event_time)}] (episode: {best.id}; session: {best.session_id}; role={best.role}) {best.text}'
                return SearchResult(query.text, via='digest', _answer=answer, limitations=flags)
    return SearchResult(query.text, facts, [score for _, score in chosen],
                        via='abstain' if abstained else 'hybrid', abstained=abstained, limitations=flags)


def context(view, query, config, semantic, *, n_facts, n_chunks, n_digests, char_budget):
    graph = RetrievalGraph(view, query)
    batch = view.candidates(query, config=config, graph=graph)
    ranked, _ = _score(batch.candidates, config, semantic, query)
    facts = [fact for fact, _ in ranked[:n_facts]]
    blocks = []
    flags = [*batch.limitations, *graph.limitations()]
    if facts:
        blocks.append('FACTS\n' + '\n'.join(f'- [{fmt_date(fact.valid_at)}] {fact.text}' for fact in facts))
    evidence = {}
    include_sources = not query.facts.exclude_sensitive and bool(n_chunks or n_digests)
    if include_sources:
        for field, count in (('text', n_chunks), ('digest', n_digests)):
            if count:
                sources = source_search(view, query, count, field=field)
                flags.extend(sources.limitations)
                evidence.update((ep.id, ep) for ep in sources.episodes)
        # Explicit provenance can surface the source even before its vector is ready.
        ids = tuple(dict.fromkeys(key for fact in facts for key in fact.provenance))
        for record in view.sources(ids, known_at=query.facts.known_at):
            ep = event_evidence(domain(record))
            if ((query.facts.valid_at is None or (ep.event_time is not None and ep.event_time <= query.facts.valid_at))
                    and (query.facts.known_at is None or ep.ingested_at <= query.facts.known_at)):
                evidence.setdefault(ep.id, ep)
        shown = ([ep for ep in evidence.values() if not ep.derived][:n_chunks]
                 + [ep for ep in evidence.values() if ep.derived][:n_digests])
        if shown:
            blocks.append('SOURCE EVIDENCE\n' + '\n'.join(
                f'- [{fmt_date(ep.event_time)}] (episode: {ep.id}; session: {ep.session_id}; role={ep.role}) {ep.text}'
                for ep in shown))
    return ContextResult('\n\n'.join(blocks)[:char_budget],
                         {view.snapshot.context.space_id: tuple(dict.fromkeys(flags))},
                         tuple(fact.id for fact in facts))


def aggregate_facts(view, scope, operation, start, end, evidence_query):
    from .structured import summarize_matches

    def rows():
        cursor = None
        while True:
            batch = view.aggregation_facts(scope, start, end, after_id=cursor)
            yield from (domain(record).as_known_at(scope.known_at) for record in batch)
            if len(batch) < 1000:
                return
            cursor = batch[-1].ref.record_id

    result = summarize_matches(rows(), operation, scope.limit)
    if not result.count and evidence_query is not None and not scope.exclude_sensitive:
        sources = source_search(view, evidence_query, scope.limit, start=start, end=end)
        flags = tuple(dict.fromkeys(('source_candidates_only', *sources.limitations)))
        return replace(result, candidates=sources.episodes, limitations=flags)
    return result


def follow_path(view, scope, anchor, predicates):
    from .structured import AmbiguousAnchor, PathMatch, PathResult, traverse

    graph = RetrievalGraph(view, RetrievalQuery(scope, ''))
    anchors = graph.path_anchors(anchor, predicates[0])
    if len(anchors) > 1:
        raise AmbiguousAnchor('Ambiguous anchor. Provide a canonical entity name.')
    if not anchors:
        return PathResult(())
    graph.truncated = False  # Only the explicit traversal contributes to path coverage.

    def neighbors(entity, predicate):
        edges = graph._edges((entity,), 'out', scope.limit + 1, (predicate,))
        facts = {record.ref.record_id: domain(record).as_known_at(scope.known_at)
                 for record in view.facts_by_ids(tuple(edge.fact_id for edge in edges), scope)}
        for edge in edges:
            if edge.fact_id in facts:
                target = graph._resolve(edge.object_id)
                graph._load_ids((target,))
                yield target, facts[edge.fact_id]

    paths, flags = traverse(anchors[0].id, predicates, neighbors, scope.limit)
    return PathResult(tuple(PathMatch(graph.entities[key].name, facts) for key, facts in paths),
                      tuple(dict.fromkeys((*flags, *graph.limitations()))))
