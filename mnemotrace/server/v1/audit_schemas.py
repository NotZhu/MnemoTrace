"""对外投影事实有效期变更和当次裁决的证据引用。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict

from ...application.operations import OperationStatus
from ...domain.fact import Fact
from ...domain.time import optional_utc_datetime, utc_datetime
from ...ports.storage import FactChange
from .page_cursor import SequencePageCursor
from .schemas import NonblankText, PageQuery


class FactChangesQuery(PageQuery):
    """筛选变更的敏感可见性并指定页范围。"""

    include_sensitive: bool = False


class ChangeListScope(BaseModel):
    """标识变更列表所绑定的资源和可见性。"""

    model_config = ConfigDict(extra="forbid")

    space_id: NonblankText
    resource: Literal["fact", "operation"]
    resource_id: NonblankText
    include_sensitive: bool


ChangePageCursor = SequencePageCursor[ChangeListScope]


class ValidityResponse(BaseModel):
    """变更前或变更后的业务有效区间。"""

    valid_at: datetime | None
    invalid_at: datetime | None


def _validity(fact: Fact) -> ValidityResponse:
    return ValidityResponse(
        valid_at=optional_utc_datetime(fact.valid_at),
        invalid_at=optional_utc_datetime(fact.invalid_at),
    )


class CutoffTriggerResponse(BaseModel):
    """一次设置或提前事实截止的可见依据，无来源时返回空数组。"""

    fact_id: str
    valid_end: datetime
    source_message_ids: list[str]


class FactChangeResponse(BaseModel):
    """事实变更的有效期和来源摘要。"""

    fact_id: str
    changed_at: datetime
    before: ValidityResponse | None
    after: ValidityResponse
    cutoff_triggers: list[CutoffTriggerResponse]

    @classmethod
    def from_change(cls, change: FactChange) -> Self:
        """将审计记录投影为公开摘要。"""
        return cls(
            fact_id=change.after.id.value,
            changed_at=utc_datetime(change.changed_at),
            before=_validity(change.before) if change.before is not None else None,
            after=_validity(change.after),
            cutoff_triggers=[
                CutoffTriggerResponse(
                    fact_id=trigger.fact_id.value,
                    valid_end=utc_datetime(trigger.valid_end),
                    source_message_ids=[
                        item.value for item in trigger.source_message_ids
                    ],
                )
                for trigger in change.cutoff_triggers
            ],
        )


class FactChangePage(BaseModel):
    """一页可见的事实变更。"""

    items: list[FactChangeResponse]
    next_cursor: str | None

    @staticmethod
    def from_changes(
        changes: Sequence[FactChange], scope: ChangeListScope, limit: int
    ) -> "FactChangePage":
        """按包含额外探测记录的查询结果构建分页响应。"""
        return FactChangePage(
            items=[
                FactChangeResponse.from_change(change) for change in changes[:limit]
            ],
            next_cursor=(
                ChangePageCursor(
                    scope=scope, position=changes[limit - 1].cursor
                ).encode()
                if len(changes) > limit
                else None
            ),
        )


class OperationFactChangePage(FactChangePage):
    """一页任务变更及其发布状态。"""

    status: OperationStatus
