"""Read entity evidence and relation definitions at the ingestion revision."""
from contextlib import contextmanager

from .errors import VersionConflict
from .records import ENTITY, ENTITY_TYPE, RELATION_TYPE, SCOPE, anchor_terms, domain


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

    def _record(self, kind, record_id):
        key = (kind, record_id)
        if key not in self._known:
            with self._read() as sql:
                row = sql.get(kind, record_id)
                self._known[key] = domain(row) if row is not None else None
        return self._known[key]

    def entity_type(self, code):
        return self._record(ENTITY_TYPE, code) if code else None

    def has_entity(self, entity_id):
        return self._record(ENTITY, entity_id) is not None

    def has_relation(self, relation_id):
        return self._record(RELATION_TYPE, relation_id) is not None

    def entities(self, names, limit):
        names = sorted({name.strip().lower() for name in names})
        with self._read() as sql:
            exact = sql.select(ENTITY, ("""(r.normalized_name = ANY(%s) OR EXISTS (
                SELECT 1 FROM mnemotrace.entity_aliases a JOIN mnemotrace.facts f
                  ON (f.user_id,f.space_id,f.id) = (a.user_id,a.space_id,a.fact_id)
                WHERE (a.user_id,a.space_id,a.entity_id) = (r.user_id,r.space_id,r.id)
                  AND a.normalized_name = ANY(%s) AND f.withdrawn_at IS NULL AND NOT f.review_pending
                ))""",), (names, names), limit=limit + 1)
            if len(exact) > limit:
                return [], False
            entities = [domain(row) for row in exact]
            terms = sorted({term for name in names for term in anchor_terms(name)})
            if terms and len(entities) < limit:
                extra = sql.select(ENTITY, ('r.anchor_terms && %s AND NOT (r.id = ANY(%s))',),
                                   (terms, [item.id for item in entities]), limit=limit - len(entities))
                entities.extend(map(domain, extra))
            candidates = []
            for entity in entities:
                snippets = sql.run(f'''SELECT text FROM mnemotrace.facts WHERE {SCOPE}
                    AND (subject_id = %s OR object_id = %s) AND withdrawn_at IS NULL AND NOT review_pending
                    ORDER BY created_at DESC, id LIMIT 2''', (*sql.scope, entity.id, entity.id)).fetchall()
                aliases = sql.run('''SELECT DISTINCT a.name FROM mnemotrace.entity_aliases a
                    JOIN mnemotrace.facts f ON (f.user_id,f.space_id,f.id) = (a.user_id,a.space_id,a.fact_id)
                    WHERE a.user_id = %s AND a.space_id = %s AND a.entity_id = %s
                      AND NOT f.review_pending AND f.withdrawn_at IS NULL ORDER BY a.name LIMIT 4''',
                    (*sql.scope, entity.id)).fetchall()
                candidates.append((entity, [item[0] for item in snippets], [item[0] for item in aliases]))
            return candidates, True

    def relations(self, predicate, meaning, left, right, limit):
        from psycopg.types.json import Jsonb

        signatures = [[left, right]] + ([[right, left]] if right is not None and right != left else [])
        clause = '(' + ' OR '.join('r.endpoint_types @> %s' for _ in signatures) + ')'
        terms = sorted(set(anchor_terms(predicate.replace('_', ' ') + ' ' + meaning)))
        # Rank endpoint-compatible labels by exact code and lexical evidence before applying the bound.
        order = """(r.predicate = %s) DESC, (SELECT count(*) FROM (
            SELECT unnest(regexp_split_to_array(lower(r.predicate || ' ' || r.meaning), '[[:space:]_[:punct:]]+'))
            INTERSECT SELECT unnest(%s::text[])) AS overlap) DESC, r.id"""
        with self._read() as sql:
            params = (*[Jsonb([signature]) for signature in signatures], predicate, terms)
            records = sql.select(RELATION_TYPE, (clause,), params, order_by=order, limit=limit)
            occupied = sql.run(f'SELECT 1 FROM mnemotrace.relation_types WHERE {SCOPE} AND predicate = %s LIMIT 1',
                               (*sql.scope, predicate)).fetchone() is not None
        return list(map(domain, records)), occupied
