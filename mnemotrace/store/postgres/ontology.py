"""Read exact names and relation definitions at the ingestion revision."""
from contextlib import contextmanager

from .errors import VersionConflict
from ...consolidate.fact_rules import entity_name_key
from ...retrieve.lexical import bm25_query_terms
from .records import ENTITY, ENTITY_TYPE, RELATION_TYPE, domain


class OntologyWorkset:
    def __init__(self, dao, snapshot):
        self.dao = dao
        self.snapshot = snapshot
        self._known = {}

    @contextmanager
    def _read(self):
        with self.dao.read_snapshot() as view:
            if (view.snapshot.core_revision, view.snapshot.space_generation) != (
                self.snapshot.core_revision, self.snapshot.space_generation,
            ):
                raise VersionConflict('Ontology changed during ingestion')
            yield view._sql

    def types(self, view=None):
        if view is not None:
            return {row.ref.record_id: domain(row) for row in view._sql.select(ENTITY_TYPE)}
        with self._read() as sql:
            return {row.ref.record_id: domain(row) for row in sql.select(ENTITY_TYPE)}

    def entities(self, names):
        if not names:
            return {}
        with self._read() as sql:
            return {entity_name_key(entity.name): entity for entity in map(domain, sql.select(
                ENTITY, ('r.normalized_name = ANY(%s)',), (sorted(names),)))}

    def relation_named(self, predicate):
        if predicate not in self._known:
            with self._read() as sql:
                rows = sql.select(RELATION_TYPE, ('r.predicate = %s',), (predicate,), limit=1)
                self._known[predicate] = (domain(rows[0]), rows[0]) if rows else None
        return self._known[predicate]

    def relations(self, meaning, left, right, limit):
        from psycopg.types.json import Jsonb

        signatures = [[left, right]] + ([[right, left]] if right is not None and right != left else [])
        fits = '(' + ' OR '.join('r.endpoint_types @> %s' for _ in signatures) + ')'
        terms = sorted(bm25_query_terms(meaning))
        overlap = """(SELECT count(*) FROM unnest(%s::text[]) AS search(term)
            WHERE strpos(lower(replace(r.predicate, '_', ' ') || ' ' || r.meaning), search.term) > 0)"""
        with self._read() as sql:
            endpoints = tuple(Jsonb([pair]) for pair in signatures)
            typed = sql.select(RELATION_TYPE, (fits,), (*endpoints, terms),
                               order_by=f'{overlap} DESC, r.id', limit=limit)
            related = sql.select(RELATION_TYPE, (f'NOT {fits}', f'{overlap} > 0'), (*endpoints, terms, terms),
                                 order_by=f'{overlap} DESC, r.id', limit=limit) if terms else ()
            return {row.ref.record_id: (domain(row), row) for row in (*typed, *related)}
