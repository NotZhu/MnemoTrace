"""Structured reads on one authorized, repeatable database snapshot."""
from dataclasses import fields

from ..contracts import FactQuery, FactState, SpaceSettings
from .errors import DataCorruption
from .records import (
    CONFLICT, ENTITY, FACT, RELATION, SOURCE, WORKING, SCOPE, TABLES, timestamp,
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

    def sources(self, source_ids):
        return self._sql.select(SOURCE, ('r.id = ANY(%s)',), (list(source_ids),))

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
        return clauses, args

    def facts(self, query: FactQuery = FactQuery()):
        clauses, args = self._fact_conditions(query)
        page(clauses, args, query.after_id, query.limit)
        return self._sql.select(FACT, clauses, args, limit=query.limit)

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

    def clearable_slot(self, subject, predicate):
        clauses, args = self._fact_conditions(FactQuery())
        clauses.extend(("r.source <> 'user'", 'r.subject = %s',
                        'btrim(coalesce(r.predicate_key, lower(r.predicate))) = %s'))
        args.extend((subject, predicate.strip().lower()))
        return self._sql.select(FACT, clauses, args)

    def working(self, *, session_id=None, kind=None, as_of=None, after_id=None, limit=100):
        clauses = ['NOT r.consumed', '(r.expires_at IS NULL OR r.expires_at > %s)']
        args = [self.at if as_of is None else timestamp(as_of)]
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
