"""定义可在 HTTP 测试中替换的应用依赖。"""

from collections.abc import Sequence
from datetime import tzinfo
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from ..application.operations import Operation, OperationStatus
from ..application.profile_refresh import PendingRefreshStatus, ProfileRefreshState
from ..application.remember import MessageInput
from ..domain.ids import SessionId, SpaceId
from ..domain.profile import Profile
from ..ports.runtime import Clock
from ..ports.storage import FactChange


class OperationAccess(Protocol):
    """提交写入任务，并在授权空间内读取任务及其事实变更。"""

    async def submit(
        self,
        *,
        owner_id: str,
        space_id: SpaceId,
        session_id: SessionId,
        key: str,
        messages: tuple[MessageInput, ...],
        extraction_timezone: tzinfo,
        queue_limit: int,
        configured: bool,
    ) -> Operation: ...

    async def list(
        self,
        owner_id: str,
        space_id: SpaceId,
        *,
        status: OperationStatus | None,
        session_id: SessionId | None,
        after: int | None,
        limit: int,
    ) -> Sequence[tuple[int, Operation]]: ...

    async def get(
        self, owner_id: str, space_id: SpaceId, operation_id: str
    ) -> Operation: ...

    async def list_fact_changes(
        self,
        owner_id: str,
        space_id: SpaceId,
        operation_id: str,
        *,
        after: int | None,
        limit: int,
        exclude_sensitive: bool,
    ) -> tuple[Operation, Sequence[FactChange]]: ...


class ReadProfile(Protocol):
    """在授权读快照中读取可见画像及其刷新状态。"""

    async def __call__(
        self, owner_id: str, space_id: SpaceId, *, clock: Clock
    ) -> tuple[Profile | None, ProfileRefreshState]: ...


class SubmitProfileRefresh(Protocol):
    """为授权空间提交手动画像刷新。"""

    async def __call__(
        self, owner_id: str, space_id: SpaceId, *, configured: bool
    ) -> PendingRefreshStatus: ...


async def business_clock(request: Request) -> Clock:
    """取得应用装配的业务时钟。"""
    return cast(Clock, request.app.state.clock)


async def operation_access(request: Request) -> OperationAccess:
    """取得写入任务依赖。"""
    return cast(OperationAccess, request.app.state.operations)


async def profile_reader(request: Request) -> ReadProfile:
    """取得一致读取画像和刷新状态的依赖。"""
    return cast(ReadProfile, request.app.state.read_profile)


async def profile_refresh_submitter(request: Request) -> SubmitProfileRefresh:
    """取得手动提交画像刷新的依赖。"""
    return cast(SubmitProfileRefresh, request.app.state.submit_profile_refresh)


ClockDependency = Annotated[Clock, Depends(business_clock)]
OperationsDependency = Annotated[OperationAccess, Depends(operation_access)]
ProfileReaderDependency = Annotated[ReadProfile, Depends(profile_reader)]
ProfileRefreshDependency = Annotated[
    SubmitProfileRefresh, Depends(profile_refresh_submitter)
]
