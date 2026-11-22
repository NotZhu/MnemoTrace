"""构造带空间作用域的事实查询条件。"""

from psycopg import sql

from ...domain._validation import timestamp
from ...domain.errors import DomainError
from ...domain.ids import SpaceId
from ...ports.queries import FactFilter


class FactConditions:
    """保存事实别名 f 的查询条件与参数。

    Args:
        space_id: 查询限定的空间标识。
    """

    def __init__(self, space_id: SpaceId) -> None:
        self.params: dict[str, object] = {"space_id": space_id.value}
        self._clauses = [sql.SQL("f.space_id = %(space_id)s")]

    def add(self, clause: str, **params: object) -> None:
        """追加查询条件。

        Args:
            clause: 调用方预定义的 SQL 片段。
            **params: 与片段占位符匹配的绑定参数。
        """
        self._clauses.append(sql.SQL(clause))
        self.params.update(params)

    def where(self) -> sql.Composed:
        """组合已追加的查询条件。

        Returns:
            可放在 WHERE 关键字后的 SQL 合取条件。
        """
        return sql.SQL(" AND ").join(self._clauses)


def fact_conditions(
    space_id: SpaceId, filters: FactFilter, reference_time: float
) -> FactConditions:
    """把事实筛选参数转换为空间内的 SQL 条件。

    Args:
        space_id: 查询限定的空间标识。
        filters: 有效时点、字段及可见性条件。
        reference_time: 省略显式时点时用于判断事实状态的 UTC epoch 秒。

    Returns:
        可继续追加条件的 SQL 片段与绑定参数。

    Raises:
        ValueError: 实际用于筛选的 reference_time 不是有限的 UTC 秒数。
    """
    conditions = FactConditions(space_id)
    if filters.state != "all":
        if filters.valid_at is None:
            at = timestamp(reference_time, "reference_time", DomainError)
        else:
            at = filters.valid_at
            conditions.add("f.valid_at IS NOT NULL")
        if filters.state == "history":
            conditions.add("f.invalid_at <= %(at)s", at=at)
        else:
            conditions.add("(f.valid_at IS NULL OR f.valid_at <= %(at)s)", at=at)
            conditions.add("(f.invalid_at IS NULL OR f.invalid_at > %(at)s)")
    if filters.overlap_start is not None:
        conditions.add(
            "(f.invalid_at IS NULL OR f.invalid_at > %(overlap_start)s)",
            overlap_start=filters.overlap_start,
        )
    if filters.overlap_end is not None:
        conditions.add(
            "(f.valid_at IS NULL OR f.valid_at < %(overlap_end)s)",
            overlap_end=filters.overlap_end,
        )
    if filters.subject is not None:
        conditions.add(
            "f.subject_id = %(subject)s", subject=filters.subject.entity_id.value
        )
    if filters.predicate is not None:
        conditions.add("f.predicate = %(predicate)s", predicate=filters.predicate.value)
    if filters.exclude_slot is not None:
        subject, predicate = filters.exclude_slot
        conditions.add(
            "NOT (f.subject_id = %(excluded_subject)s AND f.predicate = %(excluded_predicate)s)",
            excluded_subject=subject.entity_id.value,
            excluded_predicate=predicate.value,
        )
    if filters.labels:
        conditions.add(
            "f.labels && %(labels)s::text[]",
            labels=[label.value for label in filters.labels],
        )
    if filters.source is not None:
        conditions.add("f.source = %(source)s", source=filters.source.value)
    if filters.provenance:
        conditions.add(
            """
            EXISTS (
                SELECT 1 FROM mnemotrace_next.fact_provenance AS p
                WHERE p.space_id = f.space_id AND p.fact_id = f.fact_id
                  AND p.message_id = ANY(%(provenance)s::text[])
            )
            """,
            provenance=[message_id.value for message_id in filters.provenance],
        )
    if filters.exclude_sensitive:
        conditions.add("NOT f.sensitive")
    return conditions
