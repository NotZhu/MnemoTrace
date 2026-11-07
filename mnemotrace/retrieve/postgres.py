"""Assemble query results from authorized, request-local Postgres reads."""
from dataclasses import dataclass, replace

from ..memory import Memory, SearchResult, _ANSWER_TYPE_MATCH, _GENERIC_ATTR_TERMS, _TEMPORAL_HISTORY_RE
from ..memory import _expected_answer_type, should_abstain
from ..store.contracts import FactQuery, FactState, RetrievalQuery
from ..store.postgres.graph_reads import RetrievalGraph
from ..store.postgres.records import FACT, SOURCE, domain
from ..store.postgres.retrieval import Corpus
from ..util import fmt_date, time_key
from .aggregate import extract_aggregation_candidates, render_aggregation_candidates
from .events import EventEvidence, event_evidence
from .evidence import plan_evidence
from .fusion import order_by_score, weighted_rrf
from .hybrid import fact_type_weight, order_positive
from .lexical import overlap_terms
from .jsonio import loads_object
from .planner import MultiHopPlanner, _PLAN_SYSTEM, _MAX_VOCAB


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
    vector = tuple(embedder.embed(text))
    if len(vector) != embedder.dim:
        raise ValueError('Embedding dimensions differ from the configured model')
    return RetrievalQuery(scope, text, vector, model)


def _score(candidates, config, semantic):
    sem = {item.record.ref.record_id: item.semantic for item in candidates}
    records = {item.record.ref.record_id: item.record for item in candidates}
    facts = {key: domain(record) for key, record in records.items()}
    if semantic:
        sem = {key: score * fact_type_weight(facts[key], config) for key, score in sem.items()}
    rankings = {'sem': order_positive(sem)}
    for name, key, order in (('lex', 'lexical', order_positive), ('graph', 'graph', order_positive),
                             ('rec', 'recency', order_by_score), ('sal', 'salience', order_by_score)):
        rankings[name] = order({item.record.ref.record_id: getattr(item, key) for item in candidates})
    weights = {name: getattr(config, f'w_{name}') for name in rankings}
    scores = weighted_rrf(rankings, weights, config.rrf_k)
    ids = sorted(scores, key=lambda key: (-scores[key], key))
    return [(facts[key], scores[key]) for key in ids], sem


def source_search(view, query, k, *, field=None):
    corpus = Corpus(view, query, SOURCE, source_field=field)
    vectors, lexical = corpus.vector_scores(), corpus.lexical_scores()
    ranked = weighted_rrf({'sem': order_positive(vectors), 'lex': order_positive(lexical)}, {})
    ordered = sorted(ranked, key=lambda key: (-ranked[key], key))
    sources = {item.ref.record_id: domain(item) for item in view.sources(tuple(ordered), known_at=query.facts.known_at)}
    evidence = tuple(event_evidence(sources[key]) for key in ordered[:k])
    return SourceSearchResult(evidence, corpus.limitations())


class _FactLookup:
    def __init__(self, view, query):
        self.view = view
        self.query = query

    def get(self, fact_id):
        rows = self.view.facts_by_ids((fact_id,), self.query)
        return domain(rows[0]) if rows else None

    def planner(self, graph, config):
        # The lookup already applies both time axes and the requested fact state.
        return MultiHopPlanner(graph, self, config, fact_is_visible=lambda *_: True)


def graph_vocabulary(view, query):
    graph = RetrievalGraph(view, query)
    names = tuple(entity.name for entity in graph.entities.values()
                  if not any(term in entity.name.lower() for term in view.mute_terms))[:_MAX_VOCAB]
    clauses, args = view._fact_conditions(query.facts)
    sql = view._sql
    where = ' AND '.join(('r.user_id = %s', 'r.space_id = %s', *clauses))
    predicates = sql.run(f"""SELECT DISTINCT lower(r.predicate) FROM mnemotrace.facts r
        WHERE {where} ORDER BY 1 LIMIT %s""", (*sql.scope, *args, _MAX_VOCAB)).fetchall()
    return names, tuple(predicate for predicate, in predicates)


def decompose(llm, query, vocabulary):
    names, predicates = vocabulary
    if not names or len(predicates) < 2:
        return None
    prompt = f"Question: {query}\nEntities: {list(names)}\nPredicates: {list(predicates)}\nJSON:"
    try:
        obj = loads_object(llm.complete(prompt, system=_PLAN_SYSTEM))
    except Exception:  # noqa: BLE001 -- optional planning falls back to deterministic traversal
        return None
    if not obj or obj.get('anchor') not in names:
        return None
    chain = obj.get('predicates')
    if not isinstance(chain, list) or not 2 <= len(chain) <= _MAX_VOCAB:
        return None
    if any(not isinstance(item, str) or item not in predicates for item in chain):
        return None
    return obj['anchor'], chain


def search(view, query, config, top_k, semantic, decomposition=None):
    graph = RetrievalGraph(view, query)
    batch = view.candidates(query, config=config, graph=graph)
    ranked, sem = _score(batch.candidates, config, semantic)
    account = view.snapshot.context.user_id
    planner = _FactLookup(view, query.facts).planner(graph, config)
    plan = planner.plan(query.text, account, decomposition=decomposition)
    if plan is None and decomposition is not None:
        plan = planner.plan(query.text, account)
    if plan is not None:
        flags = tuple(dict.fromkeys((*batch.limitations, *graph.limitations())))
        return SearchResult(query.text, plan.facts, via='multi-hop', _answer=plan.answer, limitations=flags)

    if config.temporal_history_queries and _TEMPORAL_HISTORY_RE.search(query.text):
        history = replace(query.facts, state=FactState.ALL)
        corpus = Corpus(view, replace(query, facts=history), FACT)
        seeds = set(corpus.lexical_scores()) | {fact.id for fact, _ in ranked[:top_k]}
        chain = [fact for fact in map(domain, view.fact_chain(sorted(seeds), history)) if fact.valid_at is not None]
        at = view.at.timestamp() if history.valid_at is None else history.valid_at
        known = at if history.known_at is None else history.known_at
        result = Memory._history_answer(query.text, chain, at, lambda fact: (
            (fact.invalid_at is None or fact.invalid_at > at)
            and (fact.expired_at is None or fact.expired_at > known)))
        if result:
            return replace(result, limitations=tuple(dict.fromkeys((*batch.limitations, *corpus.limitations()))))

    if config.procedural_memory:
        procedural = Memory._rank_procedural(query.text, (fact for fact, _ in ranked), 1)
        if procedural:
            fact = procedural[0]
            sessions = list(dict.fromkeys(domain(item).session_id for item in view.sources(tuple(fact.provenance))))
            label = 'sessions: ' + ', '.join(sessions[:3]) if sessions else f'fact: {fact.id}'
            pred = fact.predicate.replace('_', ' ')
            return SearchResult(query.text, [fact], [1.0], via='procedural',
                                _answer=f'[{fmt_date(fact.valid_at)}] ({label}) {fact.subject} {pred}: {fact.object}',
                                limitations=batch.limitations)

    chosen = ranked[:top_k]
    answer_type = _expected_answer_type(query.text)
    type_ok = True
    if answer_type and chosen:
        matches = [pair for pair in chosen if _ANSWER_TYPE_MATCH[answer_type](pair[0].object or pair[0].text)]
        type_ok = bool(matches)
        if matches:
            chosen = matches + [pair for pair in chosen if pair not in matches]
    facts = [fact for fact, _ in chosen]
    flags = batch.limitations
    abstained = not facts or not type_ok or should_abstain(query.text, facts, sem.values(), config.abstain_threshold)
    if abstained and config.summary_fallback and not query.facts.exclude_sensitive:
        sources = source_search(view, query, config.summary_fallback_k, field='summary')
        flags = tuple(dict.fromkeys((*flags, *sources.limitations)))
        matched = [(len(overlap_terms(query.text, ep.text) - _GENERIC_ATTR_TERMS), ep) for ep in sources.episodes]
        if matched:
            hits, best = max(matched, key=lambda row: (row[0], time_key(row[1].event_time), row[1].id))
            if hits:
                answer = f'[{fmt_date(best.event_time)}] (episode: {best.id}; session: {best.session_id}) {best.text}'
                return SearchResult(query.text, via='summary', _answer=answer, limitations=flags)
    return SearchResult(query.text, facts, [score for _, score in chosen],
                        via='abstain' if abstained else 'hybrid', abstained=abstained, limitations=flags)


def context(view, query, config, semantic, *, n_facts, n_chunks, n_summaries, char_budget, session_id,
            evidence_queries=()):
    graph = RetrievalGraph(view, query)
    batch = view.candidates(query, config=config, graph=graph)
    ranked, _ = _score(batch.candidates, config, semantic)
    need = plan_evidence(query.text, aggregation_recall_expansion=config.aggregation_recall_expansion)
    paths = []
    if need.multi_hop:
        account = view.snapshot.context.user_id
        plan = _FactLookup(view, query.facts).planner(graph, config).plan(query.text, account)
        if plan:
            paths = plan.facts
    facts = list({fact.id: fact for fact in (*paths, *(fact for fact, _ in ranked[:n_facts]))}.values())[:n_facts]
    blocks = []
    flags = [*batch.limitations, *graph.limitations()]
    if session_id and not query.facts.exclude_sensitive:
        working = tuple(map(domain, view.working(session_id=session_id, as_of=query.facts.valid_at,
                                                 known_at=query.facts.known_at)))
        if working:
            blocks.append('WORKING MEMORY\n' + '\n'.join(f'- {item.content}' for item in working))
    if facts:
        blocks.append('FACTS\n' + '\n'.join(f'- [{fmt_date(fact.valid_at)}] {fact.evidence_text}' for fact in facts))
    if need.history or need.timeline:
        chain = tuple(map(domain, view.fact_chain([fact.id for fact in facts], replace(query.facts, state=FactState.ALL))))
        if len(chain) > 1:
            chain = sorted(chain, key=lambda fact: (time_key(fact.valid_at), fact.id))
            blocks.append('FACT HISTORY\n' + '\n'.join(
                f'- [{fmt_date(fact.valid_at)}] {fact.evidence_text}' for fact in chain[:n_facts]))
    evidence = {}
    include_sources = not query.facts.exclude_sensitive and bool(n_chunks or n_summaries)
    if include_sources:
        for field, count in (('text', n_chunks), ('summary', n_summaries)):
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
                 + [ep for ep in evidence.values() if ep.derived][:n_summaries])
        if shown:
            blocks.append('SOURCE EVIDENCE\n' + '\n'.join(
                f'- [{fmt_date(ep.event_time)}] (episode: {ep.id}; session: {ep.session_id}) {ep.text}'
                for ep in shown))
    if need.aggregation:
        aggregation_facts = {fact.id: fact for fact in facts}
        for request in evidence_queries:
            related = view.candidates(request, config=config)
            flags.extend(related.limitations)
            extra, _ = _score(related.candidates, config, semantic)
            aggregation_facts.update((fact.id, fact) for fact, _ in extra[:n_facts])
            if include_sources:
                sources = source_search(view, request, max(n_chunks, n_summaries))
                flags.extend(sources.limitations)
                evidence.update((ep.id, ep) for ep in sources.episodes)
        aggregate = render_aggregation_candidates(extract_aggregation_candidates(
            query.text, list(aggregation_facts.values()), list(evidence.values())))
        if aggregate:
            blocks.insert(0, aggregate)
    return ContextResult('\n\n'.join(blocks)[:char_budget],
                         {view.snapshot.context.space_id: tuple(dict.fromkeys(flags))},
                         tuple(fact.id for fact in facts))
