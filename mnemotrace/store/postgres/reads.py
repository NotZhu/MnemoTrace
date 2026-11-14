"""Structured reads on one authorized, repeatable database snapshot."""
from dataclasses import fields
import json

from ...focus import selectors
from ...util import name_key

from ..contracts import FactQuery, FactState, RetrievalQuery, SpaceSettings
from .errors import DataCorruption
from .records import (
    ENTITY, FACT, RELATION, SOURCE, ROW_SCOPE, SCOPE, TABLES, source_digest_join, timestamp, unpack,
)


PUBLIC_NAME_EVIDENCE = """EXISTS (SELECT 1 FROM mnemotrace.facts f
    WHERE (f.user_id,f.space_id) = (r.user_id,r.space_id) AND NOT f.sensitive
      AND ((f.subject_id = r.id AND f.subject_key = r.normalized_name)
        OR (f.object_id = r.id AND f.object_key = r.normalized_name)))"""


def page(conditions, values, after_id, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Expected page size from 1 to 1000')
    if after_id is not None:
        conditions.append('r.id > %s')
        values.append(after_id)


class ReadView:
    def __init__(self, sql, snapshot, at, settings, *, apply_focus=False):
        self._sql = sql
        self.snapshot = snapshot
        self.at = at
        self._settings = settings
        self.mute_terms = self.track_terms = ()
        if apply_focus:
            focus = self.settings()
            self.mute_terms = tuple(term.lower() for term in focus.mute)
            self.track_terms = tuple(term.lower() for term in focus.track)

    def focused(self):
        return ReadView(self._sql, self.snapshot, self.at, self._settings, apply_focus=True)

    @staticmethod
    def focus_condition(column, terms, *, categories=None):
        patterns, labels = selectors(terms)
        sql = f'lower({column}) ~ ANY(%s::text[])'
        args = [patterns]
        if categories is not None:
            sql = f'({sql} OR {categories} && %s::text[])'
            args.append(labels)
        return sql, args

    def exclude_muted(self, clauses, args, *columns, categories=None):
        if self.mute_terms:
            for column in columns:
                condition, params = self.focus_condition(column, self.mute_terms, categories=categories)
                clauses.append('NOT (' + condition + ')')
                args.extend(params)

    def get(self, kind, record_id):
        return self._sql.get(kind, record_id)

    def sources(self, source_ids):
        clauses, args = ['r.id = ANY(%s)'], [list(source_ids)]
        self.exclude_muted(clauses, args, 'r.content', "coalesce(s.digest, '')")
        return self._sql.select(SOURCE, clauses, args)

    def digests(self, source_ids):
        from ...retrieve.events import digest_evidence

        if not source_ids:
            return {}
        joins, join_params = source_digest_join()
        rows = self._sql.run(f"""SELECT r.id,r.user_id,r.session_id,r.role,r.event_time,r.ingested_at,
            s.digest,r.metadata->>'date',s.digest_coverage
            FROM mnemotrace.sources r{joins}
            WHERE {ROW_SCOPE} AND r.id = ANY(%s) AND s.digest <> ''
                AND s.content_version = r.content_version""",
            (*join_params, *self._sql.scope, list(source_ids))).fetchall()
        return {key: digest_evidence(id=key, user_id=user, session_id=session, role=role,
                                     event_time=event_time.timestamp() if event_time else None,
                                     ingested_at=ingested_at.timestamp(), digest=text, date=date or '',
                                     coverage=coverage)
                for key, user, session, role, event_time, ingested_at, text, date, coverage in rows}

    def visible_source_ids(self, ids):
        if not ids:
            return ()
        clauses, args = ['r.id = ANY(%s)'], [list(ids)]
        joins, join_params = source_digest_join()
        self.exclude_muted(clauses, args, 'r.content', "coalesce(s.digest, '')")
        where = ' AND '.join((ROW_SCOPE, *clauses))
        found = {key for key, in self._sql.run(
            f'SELECT r.id FROM mnemotrace.sources r{joins} WHERE {where}',
            (*join_params, *self._sql.scope, *args)).fetchall()}
        return tuple(key for key in ids if key in found)

    def digest_version(self, source_id):
        row = self._sql.run(f'SELECT digest_version FROM mnemotrace.source_digests WHERE {SCOPE} '
                            'AND source_id = %s', (*self._sql.scope, source_id)).fetchone()
        # A cleared digest retains the version reserved for its replacement.
        return row[0] if row else 1

    def list_sources(self, *, session_id=None, after_id=None, limit=100):
        conditions, values = [], []
        if session_id is not None:
            conditions.append('r.session_id = %s')
            values.append(session_id)
        page(conditions, values, after_id, limit)
        return self._sql.select(SOURCE, conditions, values, limit=limit)

    def _fact_conditions(self, query, *, evaluation_at=None):
        at = self.at if evaluation_at is None else timestamp(evaluation_at)
        valid = at if query.valid_at is None else timestamp(query.valid_at)
        start = 'r.valid_at <= %s'
        if query.valid_at is None:
            start = f'(r.valid_at IS NULL OR {start})'
        clauses = [start]
        args = [valid]
        if query.state != FactState.ALL:
            ended = '(r.invalid_at IS NOT NULL AND r.invalid_at <= %s)'
            clauses.append(f'NOT {ended}' if query.state == FactState.CURRENT else ended)
            args.append(valid)
        if query.categories:
            clauses.append('r.categories && %s::text[]')
            args.append(list(query.categories))
        for name in ('subject', 'predicate'):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f'r.{name}_key = %s')
                args.append(name_key(value))
        if query.source_ids:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.fact_sources p
                WHERE (p.user_id,p.space_id,p.fact_id) = (r.user_id,r.space_id,r.id)
                AND p.source_id = ANY(%s))""")
            args.append(list(query.source_ids))
        if query.exclude_sensitive:
            clauses.append('NOT r.sensitive')
        self.exclude_muted(clauses, args, 'r.text', categories='r.categories')
        return clauses, args

    def facts(self, query: FactQuery = FactQuery()):
        clauses, args = self._fact_conditions(query)
        page(clauses, args, query.after_id, query.limit)
        return self._sql.select(FACT, clauses, args, limit=query.limit,
                                hide_sensitive_predecessors=query.exclude_sensitive)

    def aggregation_facts(self, query, start, end, *, after_id=None):
        clauses, args = self._fact_conditions(query)
        if start is not None:
            clauses.append('r.valid_at >= %s')
            args.append(timestamp(start))
        if end is not None:
            clauses.append('r.valid_at < %s')
            args.append(timestamp(end))
        page(clauses, args, after_id, 1000)
        return self._sql.select(FACT, clauses, args, limit=1000,
                                hide_sensitive_predecessors=query.exclude_sensitive)

    def facts_by_ids(self, ids, query):
        clauses, args = self._fact_conditions(query)
        return self._sql.select(FACT, (*clauses, 'r.id = ANY(%s)'), (*args, list(ids)),
                                hide_sensitive_predecessors=query.exclude_sensitive)

    def slot_facts(self, query):
        clauses, args = self._fact_conditions(query)
        return self._sql.select(FACT, clauses, args,
                                hide_sensitive_predecessors=query.exclude_sensitive)

    def fact_chain(self, ids, query):
        if not ids:
            return ()
        clauses, args = self._fact_conditions(query)
        where = ' AND '.join((ROW_SCOPE, *clauses))
        rows = self._sql.run(f"""WITH RECURSIVE visible AS NOT MATERIALIZED (
            SELECT r.id FROM mnemotrace.facts r WHERE {where}
        ), chain(id) AS (
            SELECT id FROM visible WHERE id = ANY(%s)
            UNION
            SELECT r.id FROM chain c JOIN mnemotrace.fact_supersedes l
              ON l.user_id = %s AND l.space_id = %s AND c.id IN (l.fact_id, l.predecessor_id)
            JOIN visible r ON r.id = CASE WHEN c.id = l.fact_id THEN l.predecessor_id ELSE l.fact_id END
        ) SELECT id FROM chain""", (*self._sql.scope, *args, list(ids), *self._sql.scope)).fetchall()
        return self.facts_by_ids([row[0] for row in rows], query)

    def candidates(self, query: RetrievalQuery, *, config=None, graph=None):
        from .retrieval import fact_candidates
        return fact_candidates(self, query, config, graph)

    def history(self, subject, predicate=None, *, source_ids=()):
        clauses, args = self._fact_conditions(FactQuery(
            subject=subject, predicate=predicate, source_ids=tuple(source_ids), state=FactState.ALL,
        ))
        # The full slot timeline includes scheduled facts.
        return self._sql.select(FACT, clauses[1:], args[1:])

    def merge_history(self, incoming, *, symmetric=()):
        ids = sorted({fact.subject_id for fact in incoming if fact.subject_id})
        names = sorted({fact.subject_key[1] for fact in incoming if not fact.subject_id})
        endpoints = sorted({(fact.relation_id, entity_id) for fact in incoming
                            if fact.relation_id in symmetric and fact.subject_id and fact.object_id
                            for entity_id in (fact.subject_id, fact.object_id)})
        relations, peers = zip(*endpoints) if endpoints else ((), ())
        return self._sql.select(FACT, ("""(r.subject_id = ANY(%s) OR
            (r.subject_id IS NULL AND r.subject_key = ANY(%s)) OR
            EXISTS (SELECT 1 FROM unnest(%s::text[], %s::text[]) AS e(relation_id, entity_id)
                    WHERE e.relation_id = r.relation_id AND e.entity_id IN (r.subject_id, r.object_id)))""",),
            (ids, names, list(relations), list(peers)))

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
            SELECT r.id, r.sequence, sum(octet_length(coalesce(nullif(s.digest, ''), r.content)))
                OVER (ORDER BY r.sequence DESC) AS bytes,
                bool_and(coalesce(s.processing_state = 'completed', false)
                    AND (r.extract,r.source_kind) IS NOT DISTINCT FROM (%s,%s))
                OVER (ORDER BY r.sequence DESC) AS contiguous
            FROM mnemotrace.sources r LEFT JOIN mnemotrace.source_digests s
            ON (r.user_id,r.space_id,r.id) = (s.user_id,s.space_id,s.source_id)
            WHERE r.user_id = %s AND r.space_id = %s AND r.session_id = %s AND r.sequence < %s
            ORDER BY r.sequence DESC LIMIT %s)
            SELECT id FROM preceding WHERE contiguous AND bytes <= %s ORDER BY sequence""",
            (episode.extract, episode.source_kind, *self._sql.scope,
             episode.session_id, episode.sequence, budget, budget)).fetchall()
        ids = tuple(row[0] for row in rows)
        records = {record.ref.record_id: record for record in self.sources(ids)}
        return tuple(records[source_id] for source_id in ids)

    def clearable_slot(self, subject, predicate):
        clauses, args = self._fact_conditions(FactQuery())
        clauses.extend(('r.subject_key = %s', 'r.predicate_key = %s'))
        args.extend((name_key(subject), name_key(predicate)))
        return self._sql.select(FACT, clauses, args)

    def entity(self, name, *, share_safe=False):
        key = name_key(name)
        clauses, args = ['r.normalized_name = %s'], [key]
        if share_safe:
            clauses.append(PUBLIC_NAME_EVIDENCE)
        else:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.facts f
                WHERE (f.user_id,f.space_id) = (r.user_id,r.space_id) AND r.id IN (f.subject_id, f.object_id))""")
        return next(iter(self._sql.select(ENTITY, clauses, args)), None)

    def public_entity_names(self, ids):
        if not ids:
            return {}
        rows = self._sql.run('''SELECT requested.id, evidence.name
            FROM unnest(%s::text[]) AS requested(id)
            CROSS JOIN LATERAL (SELECT CASE WHEN f.subject_id = requested.id THEN f.subject ELSE f.object END AS name
                FROM mnemotrace.facts f WHERE f.user_id = %s AND f.space_id = %s
                  AND (f.subject_id = requested.id OR f.object_id = requested.id)
                  AND NOT f.sensitive
                ORDER BY f.created_at DESC, f.id LIMIT 1) AS evidence''',
            (list(ids), *self._sql.scope)).fetchall()
        return dict(rows)

    def relations(self, fact_id):
        return self._sql.select(RELATION, ('r.fact_id = %s',), (fact_id,))

    def graph_edges(self, entity_ids, query, *, direction='both', predicates=(), limit=None):
        endpoints = {'out': "(g.subject_id = ANY(%s) OR (t.direction = 'symmetric' AND g.object_id = ANY(%s)))",
                     'in': "(g.object_id = ANY(%s) OR (t.direction = 'symmetric' AND g.subject_id = ANY(%s)))",
                     'both': '(g.subject_id = ANY(%s) OR g.object_id = ANY(%s))'}
        if direction not in endpoints:
            raise ValueError('Expected graph direction in, out, or both')
        if not entity_ids:
            return ()
        clauses, args = self._fact_conditions(query)
        if predicates:
            clauses.append('r.predicate_key = ANY(%s)')
            args.append(list(predicates))
        where = ' AND '.join((ROW_SCOPE, endpoints[direction], *clauses))
        params = (*self._sql.scope, list(entity_ids), list(entity_ids), *args)
        rows = self._sql.rows(f"""SELECT g.* FROM mnemotrace.graph_relations g JOIN mnemotrace.facts r
            ON (r.user_id,r.space_id,r.id) = (g.user_id,g.space_id,g.fact_id)
            JOIN mnemotrace.relation_types t ON (t.user_id,t.space_id,t.id) = (r.user_id,r.space_id,r.relation_id)
            WHERE {where} ORDER BY g.id LIMIT %s""", (*params, query.limit if limit is None else limit))
        return tuple(unpack(self.snapshot.context, RELATION, row) for row in rows)

    def settings(self):
        try:
            raw = self._settings.copy()
            if set(raw) != {field.name for field in fields(SpaceSettings)}:
                raise ValueError('Missing or unknown setting')
            for key in ('track', 'mute'):
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

    def records(self, kind, *, after_id=None, limit=100, hide_sensitive_predecessors=False):
        clauses, args = [], []
        page(clauses, args, after_id, limit)
        return self._sql.select(kind, clauses, args, limit=limit,
                                hide_sensitive_predecessors=hide_sensitive_predecessors)

    def browse_facts(self, query, *, text='', offset=0, session_id=None, outcomes=None,
                     count_only=False, evaluation_at=None, hide_sensitive_predecessors=False):
        clauses, args = self._fact_conditions(query, evaluation_at=evaluation_at)
        if text:
            clauses.append("strpos(lower(concat_ws(' ', r.text,r.subject,r.predicate,r.object,array_to_string(r.categories, ' '))), %s) > 0")
            args.append(text.lower())
        if session_id is not None:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.fact_sources p
                JOIN mnemotrace.sources s ON (s.user_id,s.space_id,s.id) = (p.user_id,p.space_id,p.source_id)
                WHERE (p.user_id,p.space_id,p.fact_id) = (r.user_id,r.space_id,r.id) AND s.session_id = %s)""")
            args.append(session_id)
        if outcomes is not None:
            from ...consolidate.outcomes import OUTCOME_PREDICATES
            clauses.append('r.predicate_key = ANY(%s)' if outcomes else 'NOT (r.predicate_key = ANY(%s))')
            args.append(sorted(OUTCOME_PREDICATES))
        records = () if count_only else self._sql.select(
            FACT, clauses, args, limit=query.limit, offset=offset,
            order_by='r.valid_at DESC NULLS LAST, r.created_at DESC, r.id',
            hide_sensitive_predecessors=query.exclude_sensitive or hide_sensitive_predecessors)
        total = self._count('facts r', clauses, args)
        return records, total

    def fact_categories(self, *, include_sensitive, outcomes):
        clauses, args = [ROW_SCOPE], list(self._sql.scope)
        if not include_sensitive:
            clauses.append('NOT r.sensitive')
        if outcomes is not None:
            from ...consolidate.outcomes import OUTCOME_PREDICATES
            clauses.append('r.predicate_key = ANY(%s)' if outcomes else 'NOT (r.predicate_key = ANY(%s))')
            args.append(sorted(OUTCOME_PREDICATES))
        where = ' AND '.join(clauses)
        return [label for label, in self._sql.run(
            f'SELECT DISTINCT label FROM mnemotrace.facts r '
            f'CROSS JOIN LATERAL unnest(r.categories) AS categories(label) WHERE {where} ORDER BY label', args)]

    def browse_sources(self, *, offset=0, limit=100):
        clauses, args = [], []
        return self._sql.select(SOURCE, clauses, args, limit=limit, offset=offset), self._count('sources r', clauses, args)

    def _count(self, table, clauses, args):
        where = ' AND '.join((ROW_SCOPE, *clauses))
        return self._sql.run(f'SELECT count(*) FROM mnemotrace.{table} WHERE {where}',
                             (*self._sql.scope, *args)).fetchone()[0]

    def full_context_tokens(self, query: FactQuery):
        clauses = [SCOPE]
        args = list(self._sql.scope)
        for column, cutoff in (('event_time', query.valid_at),):
            if cutoff is not None:
                clauses.append(f'{column} <= %s')
                args.append(timestamp(cutoff))
        row = self._sql.run(f"""SELECT coalesce(sum((octet_length(convert_to(content, 'UTF8')) + 3) / 4), 0)
            FROM mnemotrace.sources WHERE {' AND '.join(clauses)}""", args).fetchone()
        return row[0]

    def agent_feed(self):
        return self._sql.run(f"""SELECT extract(epoch FROM max(ingested_at))::float8, count(DISTINCT session_id)
            FROM mnemotrace.sources WHERE {SCOPE} AND metadata->>'source' = 'agent_session'""",
            self._sql.scope).fetchone()

    def time_range(self):
        sources = self._sql.run(f"""SELECT extract(epoch FROM min(event_time))::float8,
            extract(epoch FROM max(event_time))::float8 FROM mnemotrace.sources WHERE {SCOPE}""",
            self._sql.scope).fetchone()
        facts = self._sql.run(f"""SELECT extract(epoch FROM min(valid_at))::float8,
            extract(epoch FROM max(valid_at))::float8 FROM mnemotrace.facts WHERE {SCOPE}""",
            self._sql.scope).fetchone()
        return dict(first_event_at=sources[0], last_event_at=sources[1],
                    oldest_fact_valid_at=facts[0], newest_fact_valid_at=facts[1])

    def overview(self, at):
        sql = self._sql
        counts = self.counts()
        fact_sql, args = self._fact_conditions(FactQuery(), evaluation_at=at)
        live = self._count('facts r', fact_sql, args)
        ended_sql, ended_args = self._fact_conditions(FactQuery(state=FactState.HISTORY), evaluation_at=at)
        ended = self._count('facts r', ended_sql, ended_args)
        sensitive = self._count('facts r', ('r.sensitive',), ())
        _, outcomes = self.browse_facts(FactQuery(limit=1), outcomes=True, count_only=True, evaluation_at=at)
        digest, consolidated = sql.run(f"""SELECT count(*) FILTER (WHERE digest <> ''),
            count(*) FILTER (WHERE processing_state = 'completed')
            FROM mnemotrace.source_digests WHERE {SCOPE}""", sql.scope).fetchone()
        pending, failures = sql.run("""SELECT
            count(DISTINCT p.source_id) FILTER (WHERE o.state IN ('pending','running','retry_wait')),
            count(DISTINCT p.source_id) FILTER (WHERE o.state = 'failed')
            FROM mnemotrace.operation_sources p JOIN mnemotrace.operations o USING (user_id,space_id,operation_id)
            WHERE p.user_id = %s AND p.space_id = %s AND NOT p.is_context AND o.kind = 'extract'""",
            sql.scope).fetchone()
        return dict(episodes=counts[SOURCE], episodes_consolidated=consolidated, episodes_pending=pending,
                    extraction_failures=failures, digests=digest, facts=counts[FACT], facts_live=live,
                    facts_superseded=ended, facts_sensitive=sensitive, facts_outcomes=outcomes,
                    entities=counts[ENTITY],
                    relations=counts[RELATION])

    def audit_edges(self, fact_ids):
        rows = self._sql.rows(f"""SELECT r.id, r.fact_id, r.predicate, r.valid_at, r.invalid_at,
            r.subject_id, r.object_id
            FROM mnemotrace.graph_relations r
            WHERE {ROW_SCOPE} AND r.fact_id = ANY(%s)""", (*self._sql.scope, fact_ids))
        return {row['fact_id']: row for row in rows}

    def orphan_entities(self, limit):
        where = f"""{ROW_SCOPE} AND NOT EXISTS (
            SELECT 1 FROM mnemotrace.graph_relations g
            WHERE (g.user_id,g.space_id) = (r.user_id,r.space_id)
            AND r.id IN (g.subject_id,g.object_id))
            AND NOT EXISTS (SELECT 1 FROM mnemotrace.facts f
            WHERE (f.user_id,f.space_id) = (r.user_id,r.space_id)
            AND r.id IN (f.subject_id,f.object_id))"""
        rows = self._sql.rows(f"""SELECT r.id, count(*) OVER () AS total FROM mnemotrace.entities r
            WHERE {where} ORDER BY r.id LIMIT %s""", (*self._sql.scope, limit))
        return rows, rows[0]['total'] if rows else 0

    def session_index(self, *, text='', limit=100, offset=0, session_id=None):
        args = (*self._sql.scope, text.lower(), session_id, session_id)
        where = f'{ROW_SCOPE} AND strpos(lower(r.session_id), %s) > 0 AND (%s::text IS NULL OR r.session_id = %s)'
        rows = self._sql.rows(f"""SELECT r.session_id AS id,
            e.episodes, e.episodes_consolidated, e.episodes_pending, e.extraction_failures, e.digests,
            extract(epoch FROM e.first_event_at)::float8 AS first_event_at,
            extract(epoch FROM e.last_event_at)::float8 AS last_event_at,
            f.facts_added, f.facts_sensitive
            FROM mnemotrace.sessions r
            CROSS JOIN LATERAL (
                SELECT count(*) AS episodes,
                    count(*) FILTER (WHERE ss.processing_state = 'completed') AS episodes_consolidated,
                    count(*) FILTER (WHERE core.pending) AS episodes_pending,
                    count(*) FILTER (WHERE core.failed) AS extraction_failures,
                    count(*) FILTER (WHERE ss.digest <> '') AS digests,
                    min(s.event_time) AS first_event_at, max(s.event_time) AS last_event_at
                FROM mnemotrace.sources s LEFT JOIN mnemotrace.source_digests ss
                    ON (ss.user_id,ss.space_id,ss.source_id) = (s.user_id,s.space_id,s.id)
                CROSS JOIN LATERAL (
                    SELECT bool_or(o.state IN ('pending','running','retry_wait')) AS pending,
                           bool_or(o.state = 'failed') AS failed
                    FROM mnemotrace.operation_sources p
                    JOIN mnemotrace.operations o USING (user_id,space_id,operation_id)
                    WHERE (p.user_id,p.space_id,p.source_id) = (s.user_id,s.space_id,s.id)
                        AND NOT p.is_context AND o.kind = 'extract'
                ) core
                WHERE (s.user_id,s.space_id,s.session_id) = (r.user_id,r.space_id,r.session_id)
            ) e
            CROSS JOIN LATERAL (
                SELECT count(*) AS facts_added, count(*) FILTER (WHERE fact.sensitive) AS facts_sensitive
                FROM mnemotrace.facts fact
                WHERE (fact.user_id,fact.space_id) = (r.user_id,r.space_id)
                  AND (fact.valid_at IS NULL OR fact.valid_at <= transaction_timestamp())
                  AND EXISTS (SELECT 1 FROM mnemotrace.fact_sources p JOIN mnemotrace.sources s
                    ON (s.user_id,s.space_id,s.id) = (p.user_id,p.space_id,p.source_id)
                    WHERE (p.user_id,p.space_id,p.fact_id) = (fact.user_id,fact.space_id,fact.id)
                      AND s.session_id = r.session_id)
            ) f
            WHERE {where} ORDER BY e.last_event_at DESC NULLS LAST, r.session_id LIMIT %s OFFSET %s""",
            (*args, limit, offset))
        total = self._sql.run(f'SELECT count(*) FROM mnemotrace.sessions r WHERE {where}', args).fetchone()[0]
        return rows, total

    def session_source_ids(self, session_id, ids):
        if not ids:
            return set()
        return {source_id for source_id, in self._sql.run(
            f'SELECT id FROM mnemotrace.sources WHERE {SCOPE} AND session_id = %s AND id = ANY(%s)',
            (*self._sql.scope, session_id, sorted(ids)))}

    def graph_page(self, query, *, text=''):
        clauses, args = self._fact_conditions(query)
        if text:
            key = text.lower()
            if query.exclude_sensitive:
                names = "(strpos(lower(r.subject), %s) > 0 OR strpos(lower(r.object), %s) > 0)"
                args.extend((key, key))
            else:
                names = """EXISTS (SELECT 1 FROM mnemotrace.entities e
                    WHERE (e.user_id,e.space_id) = (g.user_id,g.space_id)
                    AND e.id IN (g.subject_id,g.object_id) AND strpos(lower(e.name), %s) > 0)"""
                args.append(key)
            clauses.append(names)
        where = ' AND '.join((ROW_SCOPE, *clauses))
        rows = self._sql.rows(f"""SELECT g.* FROM mnemotrace.graph_relations g JOIN mnemotrace.facts r
            ON (r.user_id,r.space_id,r.id) = (g.user_id,g.space_id,g.fact_id)
            WHERE {where} ORDER BY g.id LIMIT %s""", (*self._sql.scope, *args, query.limit))
        ids = sorted({row[key] for row in rows for key in ('subject_id', 'object_id')})
        nodes = self._sql.select(ENTITY, ('r.id = ANY(%s)',), (ids,)) if ids else ()
        return nodes, tuple(unpack(self.snapshot.context, RELATION, row) for row in rows)

    def export_graph(self, kind, *, include_sensitive, after_id=None, limit=100):
        clauses, args = [], []
        page(clauses, args, after_id, limit)
        if not include_sensitive and kind == RELATION:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.facts f
                WHERE (f.user_id,f.space_id,f.id) = (r.user_id,r.space_id,r.fact_id) AND NOT f.sensitive)""")
        elif not include_sensitive and kind == ENTITY:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.graph_relations g JOIN mnemotrace.facts f
                ON (f.user_id,f.space_id,f.id) = (g.user_id,g.space_id,g.fact_id)
                WHERE (g.user_id,g.space_id) = (r.user_id,r.space_id)
                    AND r.id IN (g.subject_id,g.object_id) AND NOT f.sensitive)""")
        return self._sql.select(kind, clauses, args, limit=limit)
