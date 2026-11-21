"""编码事实列表的键集位置和用于后续页面的查询范围。"""

from datetime import UTC
from typing import Self

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, FiniteFloat

from ...domain.ids import FactId
from ...ports.queries import FactCursor, FactState
from .page_cursor import PageCursor
from .schemas import NonblankText


class FactListScope(BaseModel):
    """影响事实可见集合的列表请求参数。"""

    model_config = ConfigDict(extra="forbid")

    space_id: NonblankText
    state: FactState
    as_of: AwareDatetime | None
    include_sensitive: bool


class FactPageCursor(PageCursor):
    """记录上一页末尾和首次评估时间的版本化游标。"""

    scope: FactListScope
    evaluated_at: AwareDatetime
    recorded_at: FiniteFloat
    fact_id: NonblankText

    @classmethod
    def decode_for_scope(cls, token: str, scope: FactListScope) -> Self:
        """解析客户端游标并校验它仍对应当前筛选和空间。"""
        try:
            parsed = cls.decode(token)
            if parsed.scope != scope:
                raise ValueError
            return parsed.model_copy(
                update={"evaluated_at": parsed.evaluated_at.astimezone(UTC)}
            )
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, detail="invalid_cursor") from exc

    def position(self) -> FactCursor:
        """返回存储使用的确切排序位置。"""
        return FactCursor(self.recorded_at, FactId(self.fact_id))
