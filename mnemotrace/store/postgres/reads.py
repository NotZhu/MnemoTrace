"""Structured reads on one authorized, repeatable database snapshot."""
from dataclasses import fields
import json

from ..contracts import FactQuery, FactState, RetrievalQuery, SpaceSettings
from .errors import DataCorruption
from .records import (
    CONFLICT, ENTITY, FACT, RELATION, SOURCE, WORKING, ROW_SCOPE, SCOPE, TABLES, timestamp, unpack,
)


def page(conditions, values, after_id, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Expected page size from 1 to 1000')
    if after_id is not None:
        conditions.append('r.id > %s')
        values.append(after_id)


class ReadView:
    def __init__(self, sql, snapshot, at, settings):
        self._sql = sql
        self.snapshot = snapshot
        self.at = at
        self._settings = settings

    def get(self, kind, record_id):
        return self._sql.get(kind, record_id)

    def sources(self, source_ids, *, known_at=None):
        return self._sql.select(SOURCE, ('r.id = ANY(%s)',), (list(source_ids),), summary_known_at=known_at)

    def summary_version(self, source_id):
        row = self._sql.run(f'SELECT summary_version FROM mnemotrace.source_summaries WHERE {SCOPE} '
                            'AND source_id = %s', (*self._sql.scope, source_id)).fetchone()
        # A cleared summary retains the version reserved for its replacement.
        return row[0] if row else 1

    def list_sources(self, *, session_id=None, after_id=None, limit=100):
        conditions, values = [], []
        if session_id is not None:
            conditions.append('r.session_id = %s')
            values.append(session_id)
        page(conditions, values, after_id, limit)
        return self._sql.select(SOURCE, conditions, values, limit=limit)

    def _fact_conditions(self, query):
        valid = self.at if query.valid_at is None else timestamp(query.valid_at)
        known = valid if query.known_at is None else timestamp(query.known_at)
        clauses = ['(r.valid_at IS NULL OR r.valid_at <= %s)']
        args = [valid]
        if query.known_at is not None:
            clauses.append('r.created_at <= %s')
            args.append(known)
        if query.state == FactState.CURRENT:
            clauses.append('(r.invalid_at IS NULL OR r.invalid_at > %s) '
                           'AND (r.expired_at IS NULL OR r.expired_at > %s)')
            args.extend((valid, known))
        elif query.state == FactState.HISTORY:
            clauses.append('((r.invalid_at IS NOT NULL AND r.invalid_at <= %s) '
                           'OR (r.expired_at IS NOT NULL AND r.expired_at <= %s))')
            args.extend((valid, known))
        for name in ('category', 'subject', 'predicate'):
            value = getattr(query, name)
            if value is None:
                continue
            if name == 'category':
                clauses.append('r.category = %s')
                args.append(value)
            else:
                clauses.append(f'coalesce(r.{name}_key, lower(r.{name})) = %s')
                args.append(value.lower())
        if query.source_ids:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.fact_sources p
                WHERE (p.user_id,p.space_id,p.fact_id) = (r.user_id,r.space_id,r.id)
                AND p.source_id = ANY(%s))""")
            args.append(list(query.source_ids))
        if query.exclude_sensitive:
            clauses.append('NOT r.sensitive')
        return clauses, args

    def facts(self, query: FactQuery = FactQuery()):
        clauses, args = self._fact_conditions(query)
        page(clauses, args, query.after_id, query.limit)
        return self._sql.select(FACT, clauses, args, limit=query.limit)

    def facts_by_ids(self, ids, query):
        clauses, args = self._fact_conditions(query)
        return self._sql.select(FACT, (*clauses, 'r.id = ANY(%s)'), (*args, list(ids)))

    def slot_facts(self, slots, query):
        subjects, predicates = zip(*sorted(slots)) if slots else ((), ())
        clauses, args = self._fact_conditions(query)
        clauses.append("""EXISTS (SELECT 1 FROM unnest(%s::text[],%s::text[]) s(subject,predicate)
            WHERE (s.subject,s.predicate) = (coalesce(r.subject_key,lower(r.subject)),
                                            coalesce(r.predicate_key,lower(r.predicate))))""")
        return self._sql.select(FACT, clauses, (*args, list(subjects), list(predicates)))

    def fact_chain(self, ids, query):
        if not ids:
            return ()
        clauses, args = self._fact_conditions(query)
        where = ' AND '.join((ROW_SCOPE, *clauses))
        rows = self._sql.run(f"""WITH RECURSIVE visible AS NOT MATERIALIZED (
            SELECT r.id,r.supersedes FROM mnemotrace.facts r WHERE {where}
        ), chain(id) AS (
            SELECT id FROM visible WHERE id = ANY(%s)
            UNION
            SELECT r.id FROM chain c JOIN visible parent ON parent.id = c.id
            JOIN visible r ON r.id = parent.supersedes OR r.supersedes = c.id
        ) SELECT id FROM chain""", (*self._sql.scope, *args, list(ids))).fetchall()
        return self.facts_by_ids([row[0] for row in rows], query)

    def candidates(self, query: RetrievalQuery, *, config=None, graph=None):
        from .retrieval import fact_candidates
        return fact_candidates(self, query, config, graph)

    def history(self, subject, predicate=None, *, source_ids=()):
        clauses, args = self._fact_conditions(FactQuery(
            subject=subject, predicate=predicate, source_ids=tuple(source_ids), state=FactState.ALL,
        ))
        # History used for a write must also include future-dated records.
        return self._sql.select(FACT, clauses[1:], args[1:])

    def facts_for_manual_add(self, subject):
        clauses, args = self._fact_conditions(FactQuery())
        clauses.append('coalesce(r.subject_match_key, lower(btrim(r.subject))) = %s')
        args.append(subject.strip().lower())
        return self._sql.select(FACT, clauses, args)

    def merge_history(self, subjects):
        return self._sql.select(FACT, ('coalesce(r.subject_match_key, lower(btrim(r.subject))) = ANY(%s)',),
                                (list(subjects),))

    def fact_vectors(self, records, model, dimension):
        ids = [record.ref.record_id for record in records]
        rows = self._sql.run("""SELECT e.fact_id,e.vector::text FROM mnemotrace.embeddings e
            JOIN mnemotrace.facts f ON (f.user_id,f.space_id,f.id,f.content_version) =
                (e.user_id,e.space_id,e.fact_id,e.content_version)
            WHERE e.user_id = %s AND e.space_id = %s AND e.fact_id = ANY(%s)
            AND e.field = 'text' AND e.model = %s AND e.dimension = %s""",
            (*self._sql.scope, ids, model, dimension)).fetchall()
        return {fact_id: json.loads(vector) for fact_id, vector in rows}

    def preceding_sources(self, episode, budget):
        if not budget or episode.sequence is None:
            return ()
        rows = self._sql.run("""WITH preceding AS (
            SELECT r.id, r.sequence, sum(octet_length(coalesce(nullif(s.summary, ''), r.content)))
                OVER (ORDER BY r.sequence DESC) AS bytes,
                bool_and(coalesce(s.processing_state = 'completed', false)
                    AND (r.mode,r.scope,r.source_kind) IS NOT DISTINCT FROM (%s,%s,%s))
                OVER (ORDER BY r.sequence DESC) AS contiguous
            FROM mnemotrace.sources r LEFT JOIN mnemotrace.source_summaries s
            ON (r.user_id,r.space_id,r.id) = (s.user_id,s.space_id,s.source_id)
            WHERE r.user_id = %s AND r.space_id = %s AND r.session_id = %s AND r.sequence < %s
            ORDER BY r.sequence DESC LIMIT %s)
            SELECT id FROM preceding WHERE contiguous AND bytes <= %s ORDER BY sequence""",
            (episode.mode, episode.scope, episode.source_kind, *self._sql.scope,
             episode.session_id, episode.sequence, budget, budget)).fetchall()
        ids = tuple(row[0] for row in rows)
        records = {record.ref.record_id: record for record in self.sources(ids)}
        return tuple(records[source_id] for source_id in ids)

    def clearable_slot(self, subject, predicate):
        clauses, args = self._fact_conditions(FactQuery())
        clauses.extend(("r.source <> 'user'", 'r.subject = %s',
                        'btrim(coalesce(r.predicate_key, lower(r.predicate))) = %s'))
        args.extend((subject, predicate.strip().lower()))
        return self._sql.select(FACT, clauses, args)

    def working(self, *, session_id=None, kind=None, as_of=None, known_at=None, after_id=None, limit=100):
        clauses = ['NOT r.consumed', '(r.expires_at IS NULL OR r.expires_at > %s)']
        args = [self.at if as_of is None else timestamp(as_of)]
        if known_at is not None or as_of is not None:
            clauses.append('r.created_at <= %s')
            args.append(args[0] if known_at is None else timestamp(known_at))
        for name, value in (('session_id', session_id), ('kind', kind)):
            if value is not None:
                clauses.append(f'r.{name} = %s')
                args.append(value)
        page(clauses, args, after_id, limit)
        return self._sql.select(WORKING, clauses, args, limit=limit)

    def conflicts(self, *, after_id=None, limit=100, pending_only=True):
        clauses, args = [], []
        if pending_only:
            clauses.append("""r.status = 'pending' AND 2 = (
                SELECT count(*) FROM mnemotrace.facts f WHERE (f.user_id,f.space_id) = (r.user_id,r.space_id)
                AND f.id IN (r.older,r.newer) AND (f.valid_at IS NULL OR f.valid_at <= %s)
                AND (f.invalid_at IS NULL OR f.invalid_at > %s)
                AND (f.expired_at IS NULL OR f.expired_at > %s))""")
            args.extend((self.at,) * 3)
        page(clauses, args, after_id, limit)
        return self._sql.select(CONFLICT, clauses, args, limit=limit)

    def entity(self, name):
        return next(iter(self._sql.select(ENTITY, ('r.normalized_name = %s',), (name.lower(),))), None)

    def relations(self, fact_id):
        return self._sql.select(RELATION, ('r.fact_id = %s',), (fact_id,))

    def graph_edges(self, entity_ids, query, *, direction='both', predicates=(), excluded=(), limit=None):
        endpoints = {'out': 'g.subject_id = ANY(%s)', 'in': 'g.object_id = ANY(%s)',
                     'both': '(g.subject_id = ANY(%s) OR g.object_id = ANY(%s))'}
        if direction not in endpoints:
            raise ValueError('Expected graph direction in, out, or both')
        if not entity_ids:
            return ()
        clauses, args = self._fact_conditions(query)
        if predicates:
            clauses.append('lower(g.predicate) = ANY(%s)')
            args.append(list(predicates))
        if excluded:
            clauses.append('NOT (g.subject_id = ANY(%s) OR g.object_id = ANY(%s))')
            args.extend((list(excluded), list(excluded)))
        where = ' AND '.join((ROW_SCOPE, endpoints[direction], *clauses))
        params = (*self._sql.scope, *((list(entity_ids),) * (2 if direction == 'both' else 1)), *args)
        rows = self._sql.rows(f"""SELECT g.* FROM mnemotrace.graph_relations g JOIN mnemotrace.facts r
            ON (r.user_id,r.space_id,r.id) = (g.user_id,g.space_id,g.fact_id)
            WHERE {where} ORDER BY g.id LIMIT %s""", (*params, query.limit if limit is None else limit))
        return tuple(unpack(self.snapshot.context, RELATION, row) for row in rows)

    def settings(self):
        try:
            raw = self._settings.copy()
            if set(raw) != {field.name for field in fields(SpaceSettings)}:
                raise ValueError('Missing or unknown setting')
            for key in ('track', 'mute', 'subject_aliases'):
                if not isinstance(raw[key], list):
                    raise ValueError('Expected an array')
                raw[key] = tuple(raw[key])
            return SpaceSettings(**raw)
        except (AttributeError, TypeError, ValueError) as exc:
            raise DataCorruption('Invalid stored space settings') from exc

    def counts(self):
        return {
            kind: self._sql.run(f'SELECT count(*) FROM mnemotrace.{table} WHERE {SCOPE}',
                                self._sql.scope).fetchone()[0]
            for kind, table in TABLES.items()
        }

    def records(self, kind, *, after_id=None, limit=100):
        clauses, args = [], []
        page(clauses, args, after_id, limit)
        return self._sql.select(kind, clauses, args, limit=limit)
