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
    def focus_matches(column):
        return f'EXISTS (SELECT 1 FROM unnest(%s::text[]) term WHERE strpos(lower({column}), term) > 0)'

    def exclude_muted(self, clauses, args, *columns):
        if self.mute_terms:
            for column in columns:
                clauses.append('NOT ' + self.focus_matches(column))
                args.append(list(self.mute_terms))

    def get(self, kind, record_id):
        return self._sql.get(kind, record_id)

    def sources(self, source_ids, *, known_at=None):
        clauses, args = ['r.id = ANY(%s)'], [list(source_ids)]
        self.exclude_muted(clauses, args, 'r.content', "coalesce(s.summary, '')")
        return self._sql.select(SOURCE, clauses, args, summary_known_at=known_at)

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
        clauses = ['(r.valid_at IS NULL OR r.valid_at <= %s)',
                   'NOT r.review_pending AND (r.approved_at IS NULL OR r.approved_at <= %s)']
        args = [valid, known]
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
        self.exclude_muted(clauses, args, 'r.text')
        return clauses, args

    def facts(self, query: FactQuery = FactQuery()):
        clauses, args = self._fact_conditions(query)
        page(clauses, args, query.after_id, query.limit)
        return self._sql.select(FACT, clauses, args, limit=query.limit)

    def profile_facts(self, aliases, *, page_size=100):
        after_id = None
        while True:
            clauses, args = self._fact_conditions(FactQuery(exclude_sensitive=True))
            clauses.append('lower(r.subject) = ANY(%s)')
            args.append(sorted(aliases))
            page(clauses, args, after_id, page_size)
            records = self._sql.select(FACT, clauses, args, limit=page_size)
            yield from records
            if len(records) < page_size:
                return
            after_id = records[-1].ref.record_id

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

    def facts_for_manual_add(self, subject, predicate):
        return self._sql.select(FACT, (
            'coalesce(r.subject_match_key, lower(btrim(r.subject))) = %s',
            'lower(btrim(r.predicate)) = %s',
            'NOT r.review_pending AND (r.approved_at IS NULL OR r.approved_at <= %s)',
        ), (subject.strip().lower(), predicate.strip().lower(), self.at))

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
        self.exclude_muted(clauses, args, 'r.content')
        return self._sql.select(WORKING, clauses, args, limit=limit)

    def conflict_pairs(self, records):
        ids = [record.ref.record_id for record in records]
        if not ids:
            return ()
        return self._sql.run(
            f'SELECT least(older,newer),greatest(older,newer) FROM mnemotrace.conflicts WHERE {SCOPE} '
            'AND (older = ANY(%s) OR newer = ANY(%s))', (*self._sql.scope, ids, ids)).fetchall()

    def related_conflicts(self, fact_id, excluding):
        from .records import domain

        conflicts = tuple(map(domain, self._sql.select(CONFLICT, (
            'r.id <> %s AND r.status <> \'dismissed\' AND (r.older = %s OR r.newer = %s)',
        ), (excluding, fact_id, fact_id))))
        if not conflicts:
            return ()
        others = [c.newer if c.older == fact_id else c.older for c in conflicts]
        facts = {record.ref.record_id: domain(record)
                 for record in self._sql.select(FACT, ('r.id = ANY(%s)',), (others,))}
        return tuple((conflict, facts.get(other)) for conflict, other in zip(conflicts, others, strict=True))

    def conflicts(self, *, after_id=None, limit=100, pending_only=True):
        clauses, args = [], []
        if pending_only:
            clauses.append("""r.status = 'pending' AND EXISTS (
                SELECT 1 FROM mnemotrace.facts a JOIN mnemotrace.facts b
                  ON (a.user_id,a.space_id) = (b.user_id,b.space_id)
                CROSS JOIN (SELECT %s::timestamptz AS at) clock
                WHERE (a.user_id,a.space_id) = (r.user_id,r.space_id)
                  AND a.id IN (r.older,r.newer) AND b.id IN (r.older,r.newer) AND a.id <> b.id
                  AND NOT a.review_pending AND (a.approved_at IS NULL OR a.approved_at <= clock.at) AND (
                    (a.valid_at IS NULL OR a.valid_at <= clock.at)
                    AND (a.invalid_at IS NULL OR a.invalid_at > clock.at)
                    AND (a.expired_at IS NULL OR a.expired_at > clock.at)
                    AND (
                      (NOT b.review_pending AND (b.valid_at IS NULL OR b.valid_at <= clock.at)
                        AND (b.invalid_at IS NULL OR b.invalid_at > clock.at)
                        AND (b.expired_at IS NULL OR b.expired_at > clock.at)
                        AND (b.approved_at IS NULL OR b.approved_at <= clock.at))
                      OR (b.review_pending AND a.source = 'user' AND b.valid_at IS NULL)
                    )
                    OR (b.review_pending AND a.source = 'user' AND b.valid_at IS NOT NULL
                      AND (a.valid_at IS NULL OR a.valid_at <= b.valid_at)
                      AND (a.invalid_at IS NULL OR b.valid_at < a.invalid_at)
                      AND (a.invalid_at IS NOT NULL OR a.expired_at IS NULL OR a.expired_at > clock.at))
                  ))""")
            args.append(self.at)
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

    def portable_facts(self, *, after_id=None, limit=100):
        # Erasing either side cascades the conflict. An orphaned held fact has no portable review state.
        clauses = ["""(NOT r.review_pending OR EXISTS (
            SELECT 1 FROM mnemotrace.conflicts c
            WHERE (c.user_id,c.space_id) = (r.user_id,r.space_id) AND r.id IN (c.older,c.newer)
        ))"""]
        args = []
        page(clauses, args, after_id, limit)
        return self._sql.select(FACT, clauses, args, limit=limit)

    def records(self, kind, *, after_id=None, limit=100):
        clauses, args = [], []
        page(clauses, args, after_id, limit)
        return self._sql.select(kind, clauses, args, limit=limit)

    def browse_facts(self, query, *, text='', topic=None, category='', offset=0, session_id=None, outcomes=None,
                     count_only=False):
        clauses, args = self._fact_conditions(query)
        if topic is not None:
            clauses.append('r.topic = %s')
            args.append(topic)
        if category:
            clauses.append("coalesce(nullif(btrim(r.category), ''), '其他') = %s")
            args.append(category)
        if text:
            clauses.append("strpos(lower(concat_ws(' ', r.text,r.display,r.subject,r.predicate,r.object,r.category)), %s) > 0")
            args.append(text.lower())
        if session_id is not None:
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.fact_sources p
                JOIN mnemotrace.sources s ON (s.user_id,s.space_id,s.id) = (p.user_id,p.space_id,p.source_id)
                WHERE (p.user_id,p.space_id,p.fact_id) = (r.user_id,r.space_id,r.id) AND s.session_id = %s)""")
            args.append(session_id)
        if outcomes is not None:
            from ...consolidate.outcomes import OUTCOME_PREDICATES
            clauses.append('lower(r.predicate) = ANY(%s)' if outcomes else 'NOT (lower(r.predicate) = ANY(%s))')
            args.append(sorted(OUTCOME_PREDICATES))
        records = () if count_only else self._sql.select(
            FACT, clauses, args, limit=query.limit, offset=offset,
            order_by='r.valid_at DESC NULLS LAST, r.created_at DESC, r.id')
        total = self._count('facts r', clauses, args)
        return records, total

    def fact_categories(self, *, include_sensitive, outcomes):
        clauses, args = [ROW_SCOPE, 'NOT r.review_pending'], list(self._sql.scope)
        if not include_sensitive:
            clauses.append('NOT r.sensitive')
        if outcomes is not None:
            from ...consolidate.outcomes import OUTCOME_PREDICATES
            clauses.append('lower(r.predicate) = ANY(%s)' if outcomes else 'NOT (lower(r.predicate) = ANY(%s))')
            args.append(sorted(OUTCOME_PREDICATES))
        where = ' AND '.join(clauses)
        return [category for category, in self._sql.run(
            f"SELECT DISTINCT coalesce(nullif(btrim(r.category), ''), '其他') AS category "
            f'FROM mnemotrace.facts r WHERE {where} ORDER BY category', args)]

    def browse_sources(self, *, text='', offset=0, limit=100):
        clauses, args = [], []
        if text:
            clauses.append("""strpos(lower(concat_ws(' ', r.content,r.session_id,
                (SELECT s.summary FROM mnemotrace.source_summaries s
                 WHERE (s.user_id,s.space_id,s.source_id) = (r.user_id,r.space_id,r.id)))), %s) > 0""")
            args.append(text.lower())
        return self._sql.select(SOURCE, clauses, args, limit=limit, offset=offset), self._count('sources r', clauses, args)

    def _count(self, table, clauses, args):
        where = ' AND '.join((ROW_SCOPE, *clauses))
        return self._sql.run(f'SELECT count(*) FROM mnemotrace.{table} WHERE {where}',
                             (*self._sql.scope, *args)).fetchone()[0]

    def full_context_tokens(self, as_of):
        row = self._sql.run(f"""SELECT coalesce(sum((octet_length(convert_to(content, 'UTF8')) + 3) / 4), 0)
            FROM mnemotrace.sources WHERE {SCOPE} AND (%s OR event_time <= %s)""",
            (*self._sql.scope, as_of is None, timestamp(as_of))).fetchone()
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
        timestamp_at = timestamp(at)
        counts = self.counts()
        fact_sql, args = self._fact_conditions(FactQuery(valid_at=at))
        live = self._count('facts r', fact_sql, args)
        sensitive = self._count('facts r', ('r.sensitive',), ())
        _, outcomes = self.browse_facts(FactQuery(valid_at=at, limit=1), outcomes=True, count_only=True)
        summary, consolidated = sql.run(f"""SELECT count(*) FILTER (WHERE summary <> ''),
            count(*) FILTER (WHERE processing_state = 'completed')
            FROM mnemotrace.source_summaries WHERE {SCOPE}""", sql.scope).fetchone()
        working = sql.run(f"""SELECT count(*) FROM mnemotrace.working_memories
            WHERE {SCOPE} AND NOT consumed AND (expires_at IS NULL OR expires_at > %s)""",
            (*sql.scope, timestamp_at)).fetchone()[0]
        pending, failures = sql.run("""SELECT
            count(DISTINCT p.source_id) FILTER (WHERE o.state IN ('pending','running','retry_wait')),
            count(DISTINCT p.source_id) FILTER (WHERE o.state = 'failed')
            FROM mnemotrace.operation_sources p JOIN mnemotrace.operations o USING (user_id,space_id,operation_id)
            WHERE p.user_id = %s AND p.space_id = %s AND NOT p.is_context AND o.kind = 'extract'""",
            sql.scope).fetchone()
        conflicts = self._count('conflicts r', ("r.status = 'pending'",), ())
        return dict(episodes=counts[SOURCE], episodes_consolidated=consolidated, episodes_pending=pending,
                    extraction_failures=failures, summaries=summary, facts=counts[FACT], facts_live=live,
                    facts_superseded=counts[FACT] - live, facts_sensitive=sensitive, facts_outcomes=outcomes,
                    working_live=working, pending_conflicts=conflicts, entities=counts[ENTITY],
                    relations=counts[RELATION])

    def overflowing_slots(self, predicates):
        clauses, args = self._fact_conditions(FactQuery())
        where = ' AND '.join((ROW_SCOPE, *clauses, "r.source <> 'user'",
                              'btrim(coalesce(r.predicate_key, lower(r.predicate))) = ANY(%s)'))
        return self._sql.rows(f"""SELECT r.subject,
            btrim(coalesce(r.predicate_key, lower(r.predicate))) AS predicate,
            count(*) AS count
            FROM mnemotrace.facts r WHERE {where} GROUP BY 1,2 HAVING count(*) >= 3
            ORDER BY count(*) DESC, r.subject, predicate""",
            (*self._sql.scope, *args, predicates))

    def slot_samples(self, subject, predicate):
        clauses, args = self._fact_conditions(FactQuery())
        where = ' AND '.join((ROW_SCOPE, *clauses, "r.source <> 'user'", 'r.subject = %s',
                              'btrim(coalesce(r.predicate_key, lower(r.predicate))) = %s'))
        return self._sql.rows(f"""SELECT r.id AS fact_id, r.text FROM mnemotrace.facts r
            WHERE {where} ORDER BY r.id LIMIT 5""", (*self._sql.scope, *args, subject, predicate))

    def recent_source_texts(self):
        for offset in range(0, 2000, 100):
            rows = self._sql.run(f"""SELECT r.content FROM mnemotrace.sources r WHERE {ROW_SCOPE}
                ORDER BY r.ingested_at DESC, r.id LIMIT 100 OFFSET %s""", (*self._sql.scope, offset)).fetchall()
            yield from (row[0] for row in rows)
            if len(rows) < 100:
                break

    def orphan_entities(self, limit):
        where = f"""{ROW_SCOPE} AND NOT EXISTS (
            SELECT 1 FROM mnemotrace.graph_relations g
            WHERE (g.user_id,g.space_id) = (r.user_id,r.space_id)
            AND r.id IN (g.subject_id,g.object_id))"""
        rows = self._sql.rows(f"""SELECT r.name, count(*) OVER () AS total FROM mnemotrace.entities r
            WHERE {where} ORDER BY r.id LIMIT %s""", (*self._sql.scope, limit))
        return rows, rows[0]['total'] if rows else 0

    def session_index(self, *, text='', limit=100, offset=0, session_id=None):
        args = (*self._sql.scope, text.lower(), session_id, session_id)
        where = f'{ROW_SCOPE} AND strpos(lower(r.session_id), %s) > 0 AND (%s::text IS NULL OR r.session_id = %s)'
        rows = self._sql.rows(f"""SELECT r.session_id AS id,
            e.episodes, e.episodes_consolidated, e.episodes_pending, e.extraction_failures, e.summaries,
            extract(epoch FROM e.first_event_at)::float8 AS first_event_at,
            extract(epoch FROM e.last_event_at)::float8 AS last_event_at, w.working_live,
            f.facts_added, f.facts_sensitive
            FROM mnemotrace.sessions r
            CROSS JOIN LATERAL (
                SELECT count(*) AS episodes,
                    count(*) FILTER (WHERE ss.processing_state = 'completed') AS episodes_consolidated,
                    count(*) FILTER (WHERE core.pending) AS episodes_pending,
                    count(*) FILTER (WHERE core.failed) AS extraction_failures,
                    count(*) FILTER (WHERE ss.summary <> '') AS summaries,
                    min(s.event_time) AS first_event_at, max(s.event_time) AS last_event_at
                FROM mnemotrace.sources s LEFT JOIN mnemotrace.source_summaries ss
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
                SELECT count(*) AS working_live FROM mnemotrace.working_memories w
                WHERE (w.user_id,w.space_id,w.session_id) = (r.user_id,r.space_id,r.session_id)
                    AND NOT w.consumed AND (w.expires_at IS NULL OR w.expires_at > %s)
            ) w
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
            (self.at, *args, limit, offset))
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
            clauses.append("""EXISTS (SELECT 1 FROM mnemotrace.entities e
                WHERE (e.user_id,e.space_id) = (g.user_id,g.space_id)
                AND e.id IN (g.subject_id,g.object_id) AND strpos(lower(e.name), %s) > 0)""")
            args.append(text.lower())
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
