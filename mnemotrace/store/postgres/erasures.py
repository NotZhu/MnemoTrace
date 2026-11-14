"""空间作用域显式擦除和清理数据库引用。"""
from .records import SCOPE


def erase(sql, graph, refs):
    if not refs:
        return 0
    fact_ids = sorted(ref.record_id for ref in refs)
    endpoints = sql.run(f"""SELECT subject_id,object_id FROM mnemotrace.facts
        WHERE {SCOPE} AND id = ANY(%s)""", (*sql.scope, fact_ids)).fetchall()
    graph.orphans.update(entity_id for pair in endpoints for entity_id in pair if entity_id is not None)
    sql.run(f"""UPDATE mnemotrace.facts SET record_version = record_version + 1
        WHERE {SCOPE} AND id IN (SELECT fact_id FROM mnemotrace.fact_supersedes WHERE {SCOPE}
        AND predecessor_id = ANY(%s)) AND NOT (id = ANY(%s))""",
        (*sql.scope, *sql.scope, fact_ids, fact_ids))
    graph.remove_fact_edges(fact_ids)
    sql.delete('record_commit_receipts', 'segment_of IS NULL AND fact_ids && %s', (fact_ids,))
    # Preserve surviving segment outputs for the final receipt.
    sql.run(f"""UPDATE mnemotrace.record_commit_receipts
        SET fact_ids = ARRAY(SELECT id FROM unnest(fact_ids) AS ids(id) WHERE NOT (id = ANY(%s)))
        WHERE {SCOPE} AND segment_of IS NOT NULL AND fact_ids && %s""",
        (fact_ids, *sql.scope, fact_ids))
    sql.delete('operation_receipts', 'fact_ids && %s', (fact_ids,))
    return sql.delete('facts', 'id = ANY(%s)', (fact_ids,)).rowcount


def clear_space(sql):
    # Sources cascade from sessions; facts and entities are space-owned.
    for table in ('profile_states', 'record_commit_receipts', 'operations', 'facts', 'sessions', 'entities', 'relation_types', 'entity_types'):
        sql.delete(table)
    sql.run(f'UPDATE mnemotrace.memory_spaces SET space_generation = space_generation + 1 WHERE {SCOPE}',
            sql.scope)
