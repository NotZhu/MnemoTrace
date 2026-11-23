"""编码事实列表的键集位置和用于后续页面的查询范围。"""

from typing import Annotated, Self

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import Source
from ...domain.ids import FactId
from ...domain.time import EPOCH_SECONDS_LIMIT, MIN_EPOCH_SECONDS
from ...ports.queries import FactCursor, FactState
from .page_cursor import PageCursor
from .schemas import NonblankText, UtcDatetime


class FactListScope(BaseModel):
    """影响事实可见集合的列表请求参数。"""

    model_config = ConfigDict(extra="forbid")

    space_id: NonblankText
    state: FactState
    as_of: UtcDatetime | None
    include_sensitive: bool
    labels: tuple[str, ...]
    source: Source | None


class FactPageCursor(PageCursor):
    """记录上一页末尾和首次评估时间的版本化游标。"""

    scope: FactListScope
    evaluated_at: UtcDatetime
    recorded_at: Annotated[float, Field(ge=MIN_EPOCH_SECONDS, lt=EPOCH_SECONDS_LIMIT)]
    fact_id: NonblankText

    @classmethod
    def decode_for_scope(cls, token: str, scope: FactListScope) -> Self:
        """解析客户端游标并校验它仍对应当前筛选和空间。"""
        parsed = cls.decode(token)
        if parsed.scope != scope:
            raise HTTPException(422, detail="invalid_cursor")
        return parsed

    def position(self) -> FactCursor:
        """返回存储使用的确切排序位置。"""
        return FactCursor(self.recorded_at, FactId(self.fact_id))
