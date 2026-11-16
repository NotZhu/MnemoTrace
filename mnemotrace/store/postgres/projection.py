"""图投影管理。"""
import hashlib

from ...consolidate.graph_builder import projects_fact
from .records import FACT, RELATION_TYPE, SCOPE, domain, timestamp


def projection_id(prefix, value):
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()


class GraphProjection:
    def __init__(self, sql):
        """在当前事务中维护事实图投影。

        Args:
            sql: 已限定空间的写事务。
        """
        self.sql = sql
        self.orphans = set()

    def remove_fact_edges(self, fact_ids):
        """移除指定事实的关系边，并记录待清理端点。

        Args:
            fact_ids: 要移除关系边的事实 ID。
        """
        for left, right in self.sql.run(f'DELETE FROM mnemotrace.graph_relations WHERE {SCOPE}'
                ' AND fact_id = ANY(%s) RETURNING subject_id,object_id',
                (*self.sql.scope, list(fact_ids))).fetchall():
            self.orphans.update((left, right))

    def project(self, fact_id):
        """按事实和关系类型同步图中的关系边。

        Args:
            fact_id: 已写入当前事务的事实 ID。
        """
        record = self.sql.get(FACT, fact_id)
        fact = domain(record)
        old = self.sql.run(f'SELECT id,subject_id,object_id,predicate,valid_at,invalid_at,projection_version '
                           f'FROM mnemotrace.graph_relations WHERE {SCOPE} AND fact_id = %s',
                           (*self.sql.scope, fact_id)).fetchone()
        definition = domain(self.sql.get(RELATION_TYPE, fact.relation_id)) if fact.relation_id else None
        if not projects_fact(fact, definition):
            if old is not None:
                self.remove_fact_edges((fact_id,))
            return
        left, right = fact.subject_id, fact.object_id
        data = dict(subject_id=left, object_id=right, predicate=fact.predicate, fact_id=fact_id,
                    valid_at=timestamp(fact.valid_at), invalid_at=timestamp(fact.invalid_at))
        if old is not None:
            edge_id, old_left, old_right, predicate, valid_at, invalid_at, version = old
            if (old_left, old_right, predicate, valid_at, invalid_at) == (
                left, right, fact.predicate, data['valid_at'], data['invalid_at'],
            ):
                return
            self.sql.update('graph_relations', {**data, 'projection_version': version + 1},
                            'id = %s', (edge_id,))
            self.orphans.update((old_left, old_right))
        else:
            self.sql.insert('graph_relations', {
                'id': projection_id('rel_', fact_id), **data, 'projection_version': 1,
            })

    def prune(self, *, all_nodes=False):
        """清除没有被事实或关系引用的实体。

        Args:
            all_nodes: 是否检查整个空间，否则只检查受本次操作影响的实体。
        """
        if not all_nodes and not self.orphans:
            return
        candidates = 'true' if all_nodes else 'id = ANY(%s)'
        args = () if all_nodes else (sorted(self.orphans),)
        self.sql.delete('entities AS e', f"""{candidates}
            AND NOT EXISTS (SELECT 1 FROM mnemotrace.graph_relations r
                WHERE (r.user_id,r.space_id) = (e.user_id,e.space_id)
                AND (r.subject_id = e.id OR r.object_id = e.id))
            AND NOT EXISTS (SELECT 1 FROM mnemotrace.facts f
                WHERE (f.user_id,f.space_id) = (e.user_id,e.space_id)
                AND (f.subject_id = e.id OR f.object_id = e.id))""", args)
