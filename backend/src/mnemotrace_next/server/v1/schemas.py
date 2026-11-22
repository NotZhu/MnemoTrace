"""定义 HTTP 公共校验类型及空间和会话的请求与响应。"""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from fastapi import Header, HTTPException, Path, Request
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    with_config,
)
from typing_extensions import TypedDict

from ...domain._validation import require_nonblank
from ...domain.space import Space
from ...domain.time import utc_datetime
from ...ports.storage import SessionRegistration
from .resource_ids import decode_resource_id, encode_resource_id

_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


def _nonblank_text(value: str) -> str:
    require_nonblank(value, "文本")
    if "\x00" in value:
        raise ValueError("文本不能包含空字符")
    value.encode("utf-8")
    return value


def _strip_text(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _validate_rfc3339_text(value: object) -> str:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ValueError("须为带时区且最多六位小数的 RFC 3339 日期时间")
    return value


def _normalize_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("转换后的 UTC 时间超出支持范围") from None


NonblankText = Annotated[str, AfterValidator(_nonblank_text)]
ResourceId = Annotated[NonblankText, BeforeValidator(decode_resource_id), Path()]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", pattern=r"^[!-~]{1,128}$")
]
RFC3339 = Annotated[
    AwareDatetime,
    BeforeValidator(_validate_rfc3339_text),
    AfterValidator(_normalize_utc),
]

CREATED_LOCATION = {
    "headers": {
        "Location": {
            "description": "创建的资源地址",
            "schema": {"type": "string", "format": "uri-reference"},
        }
    }
}


def epoch_to_response_datetime[TimeField: str](
    field: TimeField, seconds: float | None, out_of_range: list[TimeField]
) -> datetime | None:
    """将有限的存储时刻转换为 UTC，并记录超出日历范围的响应字段。

    Args:
        field: 响应中的时间字段名。
        seconds: UTC epoch 秒数或未知时刻。
        out_of_range: 当前响应收集的越界字段。

    Returns:
        可表示的 UTC 日期时间，未知或越界时为 None。
    """
    if seconds is None:
        return None
    try:
        return utc_datetime(seconds)
    except OverflowError:
        out_of_range.append(field)
        return None


type _SpaceNameText = Annotated[
    NonblankText,
    StringConstraints(min_length=1, max_length=128),
    BeforeValidator(_strip_text),
]


class SpaceName(BaseModel):
    """创建空间时提供的名称。"""

    model_config = ConfigDict(extra="forbid")
    name: _SpaceNameText


@with_config(ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1}))
class SpacePatch(TypedDict, total=False):
    """更新空间名称或记忆衰减设置。"""

    name: _SpaceNameText
    memory_decay: Annotated[bool, Field(strict=True)]


def _require_space_update(patch: SpacePatch) -> SpacePatch:
    if not patch:
        raise ValueError("请提供要更新的空间设置")
    return patch


type SpacePatchBody = Annotated[SpacePatch, AfterValidator(_require_space_update)]


class SpaceResponse(BaseModel):
    """账号下可访问的空间。"""

    space_id: str
    name: str
    memory_decay: bool
    created_at: datetime | None
    out_of_range_fields: list[Literal["created_at"]]

    @classmethod
    def from_space(cls, space: Space) -> Self:
        """将核心的 UTC epoch 秒转为 HTTP 日期时间。"""
        out_of_range: list[Literal["created_at"]] = []
        return cls(
            space_id=encode_resource_id(space.id.value),
            name=space.name,
            memory_decay=space.memory_decay,
            created_at=epoch_to_response_datetime(
                "created_at", space.created_at, out_of_range
            ),
            out_of_range_fields=out_of_range,
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
            session_id=encode_resource_id(session.session_id.value),
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


async def require_empty_body(request: Request) -> None:
    """为删除与清空校验空正文，同时保留流式字节上限。"""
    has_body = False
    async for chunk in request.stream():
        has_body |= bool(chunk)
    if has_body:
        raise HTTPException(400, detail="unexpected_body")
