"""Maintain the graph rows affected by a business commit."""
import hashlib

from ...consolidate.graph_builder import projects_fact
from .database import MAX_IDENTIFIER_LENGTH
from .errors import DatabaseConstraintError, VersionConflict
from .records import ENTITY, FACT, SCOPE, anchor_terms, domain, timestamp


def projection_id(prefix, value):
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()


class GraphProjection:
    def __init__(self, sql):
        self.sql = sql
        self.orphans = set()

    def canonical(self, entity_id):
        seen = set()
        while True:
            if entity_id in seen:
                raise DatabaseConstraintError('Entity merge would form a cycle')
            seen.add(entity_id)
            row = self.sql.run(f'SELECT survivor_id FROM mnemotrace.entity_merges WHERE {SCOPE}'
                               ' AND merged_id = %s', (*self.sql.scope, entity_id)).fetchone()
            if not row:
                return entity_id
            entity_id = row[0]

    def entity_id(self, name):
        key = name.lower()
        row = self.sql.run(f'SELECT id FROM mnemotrace.entities WHERE {SCOPE}'
                           ' AND normalized_name = %s', (*self.sql.scope, key)).fetchone()
        if row is None:
            entity_id = projection_id('en_', key)
            self.sql.insert('entities', dict(
                id=entity_id, name=name, normalized_name=key, type='entity',
                aliases=[], anchor_terms=anchor_terms(name, ()), record_version=1, content_version=1,
            ))
        else:
            entity_id = row[0]
        return self.canonical(entity_id)

    def merge(self, change):
        source, target = change.merged.record_id, change.survivor.record_id
        if self.sql.get(ENTITY, source) is None or self.sql.get(ENTITY, target) is None:
            raise VersionConflict('Entity no longer exists')
        target = self.canonical(target)
        if source == target:
            raise DatabaseConstraintError('Entity merge would form a cycle')
        if self.canonical(source) != source:
            raise DatabaseConstraintError('Entity already has a merge decision')
        self.sql.insert('entity_merges', dict(
            merged_id=source, survivor_id=target, decision_source='user',
            decided_at=self.sql.run('SELECT transaction_timestamp()').fetchone()[0],
            record_version=1,
        ))
        self.sql.run(f"""UPDATE mnemotrace.graph_relations
            SET subject_id = CASE WHEN subject_id = %s THEN %s ELSE subject_id END,
                object_id = CASE WHEN object_id = %s THEN %s ELSE object_id END,
                projection_version = projection_version + 1
            WHERE {SCOPE} AND (subject_id = %s OR object_id = %s)""",
            (source, target, source, target, *self.sql.scope, source, source))

    def remove_fact_edges(self, fact_ids):
        for left, right in self.sql.run(f'DELETE FROM mnemotrace.graph_relations WHERE {SCOPE}'
                ' AND fact_id = ANY(%s) RETURNING subject_id,object_id',
                (*self.sql.scope, list(fact_ids))).fetchall():
            self.orphans.update((left, right))

    def project(self, fact_id):
        record = self.sql.get(FACT, fact_id)
        fact = domain(record)
        old = self.sql.run(f'SELECT id,subject_id,object_id,projection_version '
                           f'FROM mnemotrace.graph_relations WHERE {SCOPE} AND fact_id = %s',
                           (*self.sql.scope, fact_id)).fetchall()
        # The fact stores text. Only names and predicates that fit the graph schema become edges.
        graph_fields = (fact.subject, fact.subject.lower(), fact.object, fact.object.lower(), fact.predicate)
        if not projects_fact(fact) or any(len(value) > MAX_IDENTIFIER_LENGTH for value in graph_fields):
            self.remove_fact_edges((fact_id,))
            return
        left, right = self.entity_id(fact.subject), self.entity_id(fact.object)
        data = dict(subject_id=left, object_id=right, predicate=fact.predicate, fact_id=fact_id,
                    valid_at=timestamp(fact.valid_at), invalid_at=timestamp(fact.invalid_at))
        if len(old) == 1:
            edge_id, old_left, old_right, version = old[0]
            self.sql.update('graph_relations', {**data, 'projection_version': version + 1},
                            'id = %s', (edge_id,))
            self.orphans.update((old_left, old_right))
        else:
            self.remove_fact_edges((fact_id,))
            self.sql.insert('graph_relations', {
                'id': projection_id('rel_', fact_id), **data, 'projection_version': 1,
            })

    def prune(self):
        if not self.orphans:
            return
        self.sql.delete('entities AS e', """id = ANY(%s) AND NOT authoritative AND cardinality(aliases) = 0
            AND NOT EXISTS (SELECT 1 FROM mnemotrace.graph_relations r
                WHERE (r.user_id,r.space_id) = (e.user_id,e.space_id)
                AND (r.subject_id = e.id OR r.object_id = e.id))
            AND NOT EXISTS (SELECT 1 FROM mnemotrace.entity_merges m
                WHERE (m.user_id,m.space_id) = (e.user_id,e.space_id)
                AND (m.merged_id = e.id OR m.survivor_id = e.id))""", (sorted(self.orphans),))
