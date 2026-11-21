"""空间和会话的公开请求与响应。"""

from datetime import UTC, datetime
from typing import Annotated, Self

from fastapi import HTTPException, Path, Request
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

from ...domain._validation import require_nonblank
from ...domain.space import Space
from ...domain.time import utc_datetime
from ...ports.storage import SessionRegistration


def _nonblank_text(value: str) -> str:
    require_nonblank(value, "文本")
    if "\x00" in value:
        raise ValueError("文本不能包含空字符")
    return value


def _strip_text(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


NonblankText = Annotated[str, AfterValidator(_nonblank_text)]
ResourceId = Annotated[NonblankText, Path()]

CREATED_LOCATION = {
    "headers": {
        "Location": {
            "description": "创建的资源地址",
            "schema": {"type": "string", "format": "uri-reference"},
        }
    }
}


class SpaceName(BaseModel):
    """创建或改名时提供的空间名称。"""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[
        NonblankText,
        StringConstraints(min_length=1, max_length=128),
        BeforeValidator(_strip_text),
    ]


class SpaceResponse(BaseModel):
    """账号下可访问的空间。"""

    space_id: str
    name: str
    created_at: datetime

    @classmethod
    def from_space(cls, space: Space) -> Self:
        """将核心的 UTC epoch 秒转为 HTTP 日期时间。"""
        return cls(
            space_id=space.id.value,
            name=space.name,
            created_at=utc_datetime(space.created_at),
        )


class SpacePage(BaseModel):
    """按空间 ID 分页的结果。"""

    items: list[SpaceResponse]
    next_cursor: str | None


class SessionResponse(BaseModel):
    """空间内的会话登记。"""

    session_id: str
    created_at: datetime | None

    @classmethod
    def from_registration(cls, session: SessionRegistration) -> Self:
        """以 UTC 返回会话创建时间，保留未知值。"""
        created_at = session.created_at
        return cls(
            session_id=session.session_id.value,
            created_at=created_at.astimezone(UTC) if created_at is not None else None,
        )


class SessionPage(BaseModel):
    """按创建时间分页的会话登记。"""

    items: list[SessionResponse]
    next_cursor: str | None


class PageQuery(BaseModel):
    """空间、会话和事实列表共用的分页参数。"""

    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(ge=1, le=100)] = 50
    cursor: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")] | None = None


async def require_json(request: Request) -> None:
    """为接受 JSON 正文的写入入口校验媒体类型。"""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type.lower() != "application/json":
        raise HTTPException(415, detail="unsupported_media_type")
