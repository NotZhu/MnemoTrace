"""Scoped PostgreSQL queries for retrieval signals and coverage."""
from ...config import Config
from ...retrieve.hybrid import HybridRetriever
from ...retrieve.lexical import BM25_B, BM25_K1, bm25_query_terms
from ...util import recency, time_key
from ..contracts import Candidate, CandidateBatch
from .graph_reads import RetrievalGraph
from .lexical import ANALYZER
from .records import FACT, ROW_SCOPE, SOURCE, TARGET_COLUMNS, domain, source_digest_join, timestamp

_TRACK_BOOST = 0.5


class Corpus:
    def __init__(self, view, query, kind, *, source_field=None, start=None, end=None):
        self.sql = view._sql
        self.query = query
        self.kind = kind
        self.column = TARGET_COLUMNS[kind]
        facts = query.facts
        join_params = ()
        if facts.after_id is not None:
            raise ValueError('Retrieval does not accept a browsing cursor')
        if kind == FACT:
            conditions, params = view._fact_conditions(facts)
            salience = 'r.salience'
            if view.track_terms:
                salience += f' + CASE WHEN {view.focus_matches("r.text")} THEN %s ELSE 0 END'
                join_params = (list(view.track_terms), _TRACK_BOOST)
            select = f"""SELECT r.user_id,r.space_id,r.id,r.content_version,
                'text' AS field,r.content_version AS text_version,r.valid_at,{salience} AS salience
                FROM mnemotrace.facts r"""
        else:
            conditions, params = [], []
            for column, value in (('event_time', facts.valid_at), ('ingested_at', facts.known_at)):
                if value is not None:
                    conditions.append(f'r.{column} <= %s')
                    params.append(timestamp(value))
            if start is not None:
                conditions.append('r.event_time >= %s')
                params.append(timestamp(start))
            if end is not None:
                conditions.append('r.event_time < %s')
                params.append(timestamp(end))
            joins, join_params = source_digest_join(facts.known_at)
            select = """SELECT r.user_id,r.space_id,r.id,r.content_version,r.session_id,
                CASE WHEN s.digest <> '' THEN 'digest' ELSE 'text' END AS field,
                CASE WHEN s.digest <> '' THEN s.digest_version ELSE r.content_version END AS text_version
                FROM mnemotrace.sources r""" + joins + ' AND s.content_version = r.content_version'
            if source_field == 'digest':
                conditions.append("s.digest <> ''")
            elif source_field == 'text':
                conditions.append("(s.digest IS NULL OR s.digest = '')")
            view.exclude_muted(conditions, params, 'r.content', "coalesce(s.digest, '')")
        self.source_field = source_field
        scoped = select + ' WHERE ' + ' AND '.join((ROW_SCOPE, *conditions))
        self.eligible = f'WITH eligible AS ({scoped})'
        self.params = (*join_params, *self.sql.scope, *params)

    def vector_scores(self, *, ids=None):
        statement = self._vector_statement(ids)
        # Query selectivity varies sharply across scopes. A reused generic plan
        # can pick a large-space scan or join order for a small-space request.
        return dict(self.sql.run(*statement, prepare=False).fetchall()) if statement else {}

    def explain_vector(self):
        query, params = self._vector_statement(None)
        return self.sql.run('EXPLAIN (FORMAT JSON) ' + query, params, prepare=False).fetchone()[0][0]

    def _vector_statement(self, ids):
        query = self.query
        if query.vector is None or not any(query.vector):
            return None
        dimension = len(query.vector)
        literal = '[' + ','.join(map(str, query.vector)) + ']'
        extra = '' if ids is None else 'AND x.id = ANY(%s)'
        limit = query.facts.limit if ids is None else len(ids)
        text_match = '(e.field,e.content_version) = (x.field,x.text_version)'
        include_original = self.kind == SOURCE and self.source_field != 'text'
        if include_original:
            text_match = f"({text_match} OR (e.field = 'text' AND e.content_version = x.content_version))"
        filters = f"""FROM mnemotrace.embeddings e JOIN eligible x
            ON (e.user_id,e.space_id,e.{self.column}) = (x.user_id,x.space_id,x.id) AND {text_match}
            WHERE e.target_kind = %s AND e.model = %s AND e.dimension = %s
            AND public.vector_norm(e.vector) > 0 {extra}"""
        if self.kind == FACT:
            # The windowed slot filter can hide this constant from partial-index inference.
            filters += " AND e.field = 'text'"
        params = (self.kind.value, query.embedding_model, dimension, *((list(ids),) if ids is not None else ()))
        if query.exact or ids is not None:
            # Materialize the scoped vectors before distance ranking, even when a
            # test database has an approximate index over this embedding space.
            score = '1 - (vector OPERATOR(public.<=>) %s::public.vector)'
            if include_original:
                score = f'max({score})'
            group = 'GROUP BY id' if include_original else ''
            return (self.eligible + f""", scoped AS MATERIALIZED (SELECT x.id,e.vector {filters})
                SELECT id,{score} AS score FROM scoped {group} ORDER BY score DESC,id LIMIT %s""",
                (*self.params, *params, literal, limit))
        # The dimension is derived from a validated vector length.
        filters += ' AND e.user_id = %s AND e.space_id = %s'
        distance = f'e.vector::public.vector({dimension}) OPERATOR(public.<=>) %s::public.vector({dimension})'
        return (self.eligible + f""", hits AS MATERIALIZED (
            SELECT x.id, {distance} AS distance {filters} ORDER BY distance LIMIT %s)
            SELECT id,1-distance FROM hits ORDER BY distance,id""",
            (*self.params, literal, *params, *self.sql.scope, limit))

    def lexical_scores(self, *, ids=None):
        terms = sorted(bm25_query_terms(self.query.text))
        if not terms:
            return {}
        limit = self.query.facts.limit if ids is None else len(ids)
        extra = '' if ids is None else 'WHERE x.id = ANY(%s)'
        statement, params = self._lexical()
        rows = self.sql.run(statement + f""", totals AS (
                SELECT count(*) AS n, greatest(1.0,avg(document_length))::float8 AS avgdl,
                    %s::float8 AS k1,%s::float8 AS b FROM documents WHERE document_id IS NOT NULL
            ), matches AS MATERIALIZED (
                SELECT x.id,x.document_id,x.document_length,t.term,t.tf
                FROM mnemotrace.lexical_terms t JOIN documents x
                ON (t.user_id,t.space_id,t.document_id) = (x.user_id,x.space_id,x.document_id)
                WHERE t.term = ANY(%s)
            ), frequencies AS (
                SELECT term,count(*) AS df FROM matches GROUP BY term
            ), scores AS (
                SELECT x.id,sum(ln(1.0 + (s.n-f.df+0.5)::float8/(f.df+0.5)::float8)
                    * (x.tf*(s.k1+1))/(x.tf+s.k1*(1-s.b+s.b*x.document_length/s.avgdl))) AS score
                FROM matches x JOIN frequencies f USING (term) CROSS JOIN totals s
                {extra} GROUP BY x.id,x.document_id
            ) SELECT id,max(score) AS score FROM scores GROUP BY id ORDER BY score DESC,id LIMIT %s""",
            (*params, BM25_K1, BM25_B, terms, *((list(ids),) if ids is not None else ()), limit)).fetchall()
        return dict(rows)

    def _lexical(self):
        representations = 'SELECT x.field AS field, x.text_version AS version'
        if self.kind == SOURCE and self.source_field != 'text':
            representations += " UNION ALL SELECT 'text', x.content_version WHERE x.field = 'digest'"
        statement = self.eligible + f""", documents AS MATERIALIZED (
            SELECT x.user_id,x.space_id,x.id,d.id AS document_id,d.document_length
            FROM eligible x CROSS JOIN LATERAL ({representations}) p
            LEFT JOIN mnemotrace.lexical_documents d
            ON (d.user_id,d.space_id,d.{self.column}) = (x.user_id,x.space_id,x.id)
            AND (d.field,d.content_version) = (p.field,p.version)
            AND d.analyzer_version = %s
        )"""
        return statement, (*self.params, ANALYZER)

    def limitations(self):
        flags = []
        statement, params = self._lexical()
        if self.sql.run(statement + ' SELECT EXISTS(SELECT 1 FROM documents WHERE document_id IS NULL)',
                        params).fetchone()[0]:
            flags.append('lexical_backfill_pending')
        if self.query.vector is None:
            return tuple(flags)
        if self.kind == SOURCE:
            flags.extend(self._source_vector_limitations())
        elif self.sql.run(self.eligible + """ SELECT EXISTS (SELECT 1 FROM eligible x WHERE NOT EXISTS (
            SELECT 1 FROM mnemotrace.embeddings e
            WHERE (e.user_id,e.space_id,e.fact_id,e.field,e.content_version) =
                (x.user_id,x.space_id,x.id,x.field,x.text_version)
            AND e.model = %s AND e.dimension = %s))""",
            (*self.params, self.query.embedding_model, len(self.query.vector))).fetchone()[0]:
            flags.append('fact_vectors_incomplete')
        if not self.query.exact:
            flags.append('approximate_vector_search')
        return tuple(flags)

    def _source_vector_limitations(self):
        # Coverage is about original text even when the preferred evidence is a digest.
        rows = self.sql.run(self.eligible + """ SELECT DISTINCT
            CASE WHEN o.state = 'failed' THEN 'source_vectors_failed'
                 WHEN o.state IN ('pending','running','retry_wait') THEN 'source_vectors_processing'
                 ELSE 'source_vectors_pending_backfill' END
            FROM eligible x
            JOIN mnemotrace.memory_spaces s USING (user_id,space_id)
            JOIN mnemotrace.sessions session USING (user_id,space_id,session_id)
            LEFT JOIN LATERAL (
                SELECT task.state FROM mnemotrace.operation_targets t
                JOIN mnemotrace.operations task USING (user_id,space_id,operation_id)
                WHERE (t.user_id,t.space_id,t.source_id,t.content_version) =
                    (x.user_id,x.space_id,x.id,x.content_version)
                AND task.kind = 'reembed' AND task.embedding_model = %s AND task.embedding_dimension = %s
                AND task.space_generation = s.space_generation
                ORDER BY task.operation_id LIMIT 1
            ) o ON true
            WHERE NOT EXISTS (SELECT 1 FROM mnemotrace.embeddings e
                WHERE (e.user_id,e.space_id,e.source_id,e.content_version) =
                    (x.user_id,x.space_id,x.id,x.content_version)
                AND e.field = 'text' AND e.model = %s AND e.dimension = %s)
            ORDER BY 1""",
            (*self.params, *((self.query.embedding_model, len(self.query.vector)) * 2))).fetchall()
        return tuple(flag for flag, in rows)

    def scalar_candidates(self):
        limit = self.query.facts.limit
        return tuple(row[0] for row in self.sql.run(self.eligible + """
            (SELECT id FROM eligible ORDER BY valid_at DESC NULLS LAST,id LIMIT %s)
            UNION
            (SELECT id FROM eligible ORDER BY salience DESC,id LIMIT %s)""",
            (*self.params, limit, limit)).fetchall())


def fact_candidates(view, query, config=None, graph=None):
    config = config if config is not None else Config()
    graph = graph if graph is not None else RetrievalGraph(view, query)
    retriever = HybridRetriever(None, graph, None, config)
    account = view.snapshot.context.user_id
    corpus = Corpus(view, query, FACT)
    semantic, lexical = corpus.vector_scores(), corpus.lexical_scores()
    ids = set(semantic) | set(lexical) | set(corpus.scalar_candidates())
    if config.graph_proximity:
        frontier = retriever.query_entity_ids(query.text, account)
        visited = set()
        for _ in range(max(1, config.max_hops)):
            if not frontier:
                break
            visited.update(frontier)
            next_frontier = set()
            for entity_id in sorted(frontier):
                for direction in ('in', 'out'):
                    for edge in graph.neighbors(entity_id, direction=direction):
                        ids.add(edge.fact_id)
                        next_frontier.update((edge.subject_id, edge.object_id))
            frontier = next_frontier - visited
            if len(frontier) > query.facts.limit:
                graph.truncated = True
                frontier = set(sorted(frontier)[:query.facts.limit])
    records = {record.ref.record_id: record for record in view.facts_by_ids(sorted(ids), query.facts)}
    facts = list(map(domain, records.values()))
    graph_scores, _ = retriever._graph_scores(query.text, account, facts, None)
    missing_sem = [fact.id for fact in facts if fact.id not in semantic]
    missing_lex = [fact.id for fact in facts if fact.id not in lexical]
    if missing_sem:
        semantic.update(corpus.vector_scores(ids=missing_sem))
    if missing_lex:
        lexical.update(corpus.lexical_scores(ids=missing_lex))
    as_of = view.at.timestamp() if query.facts.valid_at is None else query.facts.valid_at
    candidates = tuple(Candidate(
        records[fact.id], semantic.get(fact.id, 0), lexical.get(fact.id, 0), graph_scores.get(fact.id, 0),
        recency(max(0, as_of - time_key(fact.valid_at)), config.recency_tau_days),
        fact.salience + (_TRACK_BOOST if any(term in fact.text.lower() for term in view.track_terms) else 0),
    ) for fact in sorted(facts, key=lambda fact: fact.id))
    flags = (*corpus.limitations(), *graph.limitations())
    return CandidateBatch(view.snapshot, candidates, not flags, flags)
