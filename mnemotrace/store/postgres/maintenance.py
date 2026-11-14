"""数据库维护工具。"""
from dataclasses import asdict

from ..contracts import RecordKind
from .dao import PostgresDAO
from .database import identifier
from .lexical import project_document, project_text
from .projection import GraphProjection
from .reads import page
from .records import FACT, SOURCE, domain


def _page_filters(after_id, limit):
    if after_id is not None:
        identifier(after_id)
    conditions, args = [], []
    page(conditions, args, after_id, limit)
    return conditions, args


def rebuild_lexical_index(dao: PostgresDAO, kind: RecordKind, *, after_id=None, limit=100) -> str | None:
    """Repair missing or stale lexical documents for facts or source digests."""
    if not isinstance(kind, RecordKind) or kind not in (FACT, SOURCE):
        raise ValueError('Expected a Fact or Episode RecordKind')
    conditions, args = _page_filters(after_id, limit)
    with dao._transaction() as sql:
        records = sql.select(kind, conditions, args, limit=limit)
        for record in records:
            item = domain(record)
            if kind == FACT:
                project_text(sql, FACT, item.id, record.content_version, asdict(item), missing_only=True)
            elif item.digest:
                project_document(sql, SOURCE, item.id, record.digest_version, item.digest,
                                 field='digest', missing_only=True)
    return records[-1].ref.record_id if len(records) == limit else None


def rebuild_graph(dao: PostgresDAO, *, after_id=None, limit=100) -> str | None:
    """Repair graph edges from stored facts and prune orphaned entities on the final page."""
    conditions, args = _page_filters(after_id, limit)
    with dao._transaction() as sql:
        records = sql.select(FACT, conditions, args, limit=limit)
        graph = GraphProjection(sql)
        for record in records:
            graph.project(record.ref.record_id)
        graph.prune(all_nodes=len(records) < limit)
    return records[-1].ref.record_id if len(records) == limit else None
