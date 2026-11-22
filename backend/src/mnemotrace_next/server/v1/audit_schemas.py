"""对外投影事实有效期变更和当次裁决的证据引用。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict

from ...application.operations import OperationStatus
from ...domain.fact import Fact
from ...ports.storage import FactChange
from .page_cursor import SequencePageCursor
from .resource_ids import encode_resource_id
from .schemas import NonblankText, PageQuery, epoch_to_response_datetime


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


def _validity(fact: Fact, path: str, out_of_range: list[str]) -> ValidityResponse:
    return ValidityResponse(
        valid_at=epoch_to_response_datetime(
            f"{path}/valid_at", fact.valid_at, out_of_range
        ),
        invalid_at=epoch_to_response_datetime(
            f"{path}/invalid_at", fact.invalid_at, out_of_range
        ),
    )


class CutoffTriggerResponse(BaseModel):
    """一次设置或提前事实截止的可见依据。"""

    fact_id: str
    valid_end: datetime | None
    source_message_ids: list[str] | None


class FactChangeResponse(BaseModel):
    """事实变更的有效期和来源摘要。"""

    fact_id: str
    changed_at: datetime | None
    before: ValidityResponse | None
    after: ValidityResponse
    cutoff_triggers: list[CutoffTriggerResponse]
    out_of_range_fields: list[str]

    @classmethod
    def from_change(cls, change: FactChange) -> Self:
        """将审计记录投影为公开摘要。"""
        out_of_range: list[str] = []
        return cls(
            fact_id=encode_resource_id(change.after.id.value),
            changed_at=epoch_to_response_datetime(
                "/changed_at", change.changed_at, out_of_range
            ),
            before=(
                _validity(change.before, "/before", out_of_range)
                if change.before is not None
                else None
            ),
            after=_validity(change.after, "/after", out_of_range),
            cutoff_triggers=[
                CutoffTriggerResponse(
                    fact_id=encode_resource_id(trigger.fact_id.value),
                    valid_end=epoch_to_response_datetime(
                        f"/cutoff_triggers/{index}/valid_end",
                        trigger.valid_end,
                        out_of_range,
                    ),
                    source_message_ids=(
                        [
                            encode_resource_id(item.value)
                            for item in trigger.source_message_ids
                        ]
                        if trigger.source_message_ids is not None
                        else None
                    ),
                )
                for index, trigger in enumerate(change.cutoff_triggers)
            ],
            out_of_range_fields=out_of_range,
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
