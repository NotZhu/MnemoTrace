"""Scope explicit erasure and clean up database references in the same commit."""
from collections import defaultdict

from .records import FACT, SCOPE, TABLES, TARGET_COLUMNS


def erase(sql, graph, refs):
    groups = defaultdict(set)
    for ref in refs:
        groups[ref.kind].add(ref.record_id)
    # Removing a version does not remove its surviving successors. Drop the erased link.
    fact_ids = sorted(groups[FACT])
    if fact_ids:
        sql.run(f'UPDATE mnemotrace.facts SET supersedes = NULL, record_version = record_version + 1 '
                f'WHERE {SCOPE} AND supersedes = ANY(%s) AND NOT (id = ANY(%s))',
                (*sql.scope, fact_ids, fact_ids))
        graph.remove_fact_edges(fact_ids)
    for kind, column in TARGET_COLUMNS.items():
        if groups[kind]:
            sql.delete('operations', f'operation_id IN (SELECT operation_id FROM mnemotrace.operation_targets '
                       f'WHERE {SCOPE} AND {column} = ANY(%s))', (*sql.scope, sorted(groups[kind])))
    if fact_ids:
        sql.delete('record_commit_receipts', 'segment_of IS NULL AND fact_ids && %s', (fact_ids,))
        # Preserve surviving segment outputs for the final receipt.
        sql.run(f"""UPDATE mnemotrace.record_commit_receipts
            SET fact_ids = ARRAY(SELECT id FROM unnest(fact_ids) AS ids(id) WHERE NOT (id = ANY(%s)))
            WHERE {SCOPE} AND segment_of IS NOT NULL AND fact_ids && %s""",
            (fact_ids, *sql.scope, fact_ids))
        sql.delete('operation_receipts', 'fact_ids && %s', (fact_ids,))
    count = 0
    for kind, ids in groups.items():
        if ids:
            count += sql.delete(TABLES[kind], 'id = ANY(%s)', (sorted(ids),)).rowcount
    return count


def clear_space(sql):
    # Sources cascade from sessions; facts and entities are space-owned.
    for table in ('record_commit_receipts', 'operations', 'facts', 'sessions', 'entities', 'import_receipts'):
        sql.delete(table)
    sql.run(f'UPDATE mnemotrace.memory_spaces SET space_generation = space_generation + 1 WHERE {SCOPE}',
            sql.scope)
