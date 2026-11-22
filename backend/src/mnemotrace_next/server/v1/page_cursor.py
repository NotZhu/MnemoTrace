"""编码客户端原样回传的列表游标。"""

from base64 import b64decode, urlsafe_b64encode
from typing import Annotated, Literal, Self

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .schemas import NonblankText


class PageCursor(BaseModel):
    """承载键集位置的版本化游标。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1

    @classmethod
    def decode(cls, token: str) -> Self:
        """校验游标编码及对应列表的字段结构。"""
        try:
            encoded = token + "=" * (-len(token) % 4)
            return cls.model_validate_json(
                b64decode(encoded, altchars=b"-_", validate=True), strict=True
            )
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, detail="invalid_cursor") from exc

    def encode(self) -> str:
        """生成客户端可原样重传的游标。"""
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


class SpacePageCursor(PageCursor):
    """记录空间列表的末尾标识。"""

    space_id: NonblankText


class SessionPageCursor(PageCursor):
    """记录会话列表的空间和末尾排序位置。"""

    space_id: NonblankText
    session_id: NonblankText
    created_at: AwareDatetime | None


class SequencePageCursor[Scope: BaseModel](PageCursor):
    """记录顺序分页的范围和末尾位置。"""

    scope: Scope
    position: Annotated[int, Field(ge=1, le=2**63 - 1)]

    @classmethod
    def position_for(cls, token: str | None, scope: Scope) -> int | None:
        """解析续页位置并校验资源及筛选范围。"""
        if token is None:
            return None
        cursor = cls.decode(token)
        if cursor.scope != scope:
            raise HTTPException(422, detail="invalid_cursor")
        return cursor.position
