"""读取空间的可用画像并手动请求刷新。"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...application.operations import WriteModelsUnavailable
from ...application.profile_refresh import PendingRefreshStatus, RefreshStatus
from ...domain.ids import SpaceId
from ...domain.time import utc_datetime
from ..auth import OwnerDependency
from ..dependencies import (
    ClockDependency,
    ProfileReaderDependency,
    ProfileRefreshDependency,
)
from ..errors import problem_responses
from .schemas import ResourceId, require_empty_body

router = APIRouter(
    prefix="/spaces/{space_id}/profile",
    tags=["profiles"],
    responses=problem_responses(401, 404, 422, 503),
)


class ProfileItem(BaseModel):
    """一条有事实依据的画像条目。"""

    text: str
    fact_ids: Annotated[list[str], Field(min_length=1)]


class ProfileData(BaseModel):
    """可独立读取的四组用户画像。"""

    basic: list[ProfileItem]
    communication_preferences: list[ProfileItem]
    preferences: list[ProfileItem]
    constraints: list[ProfileItem]


class ProfileRefreshError(BaseModel):
    """最近一次刷新失败的公开分类。"""

    code: str


class ProfileResponse(BaseModel):
    """在同一读快照内取得的画像与刷新状态。"""

    profile: ProfileData | None
    updated_at: datetime | None
    refresh_status: RefreshStatus
    next_retry_at: datetime | None
    refresh_error: ProfileRefreshError | None


class ProfileRefreshAccepted(BaseModel):
    """已持久接收或仍在处理的刷新。"""

    refresh_status: PendingRefreshStatus


@router.get("", operation_id="getProfile")
async def get_profile(
    space_id: ResourceId,
    owner_id: OwnerDependency,
    read_profile: ProfileReaderDependency,
    clock: ClockDependency,
) -> ProfileResponse:
    """读取仍有有效证据的画像条目和可轮询的刷新状态。"""
    profile, refresh = await read_profile(owner_id, SpaceId(space_id), clock=clock)
    data = (
        ProfileData(
            **{
                group: [
                    ProfileItem(
                        text=entry.text,
                        fact_ids=[fact_id.value for fact_id in entry.fact_ids],
                    )
                    for entry in entries
                ]
                for group, entries in profile.data.items()
            }
        )
        if profile is not None
        else None
    )
    return ProfileResponse(
        profile=data,
        updated_at=utc_datetime(profile.updated_at) if profile is not None else None,
        refresh_status=refresh.status,
        next_retry_at=refresh.next_retry_at.astimezone(UTC)
        if refresh.next_retry_at
        else None,
        refresh_error=ProfileRefreshError(code=refresh.error_code)
        if refresh.error_code
        else None,
    )


@router.post(
    "/refresh",
    operation_id="refreshProfile",
    status_code=202,
    dependencies=[Depends(require_empty_body)],
    responses={
        202: {
            "headers": {
                "Location": {
                    "description": "可轮询的画像地址",
                    "schema": {"type": "string", "format": "uri-reference"},
                }
            }
        },
    },
)
async def refresh_profile(
    space_id: ResourceId,
    request: Request,
    response: Response,
    owner_id: OwnerDependency,
    submit_refresh: ProfileRefreshDependency,
) -> ProfileRefreshAccepted:
    """请求手动刷新，已有在途刷新时复用它。"""
    try:
        status = await submit_refresh(
            owner_id, SpaceId(space_id), configured=request.app.state.write_configured
        )
    except WriteModelsUnavailable as exc:
        raise HTTPException(503, detail="write_models_unavailable") from exc
    response.headers["Location"] = str(
        request.app.url_path_for("get_profile", space_id=space_id)
    )
    return ProfileRefreshAccepted(refresh_status=status)
