"""管理账号名下的空间。"""

from time import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from ...adapters.runtime import UuidGenerator
from ...domain.ids import SpaceId
from ...domain.space import Space
from ..auth import AuthorizedStorage, OwnerDependency
from ..errors import problem_responses
from .page_cursor import SpacePageCursor
from .schemas import (
    CREATED_LOCATION,
    PageQuery,
    ResourceId,
    SpaceName,
    SpacePage,
    SpaceResponse,
    require_json,
)

router = APIRouter(
    prefix="/spaces", tags=["spaces"], responses=problem_responses(401, 422, 500, 503)
)
_ids = UuidGenerator()
_json_errors = problem_responses(400, 413, 415)


@router.post(
    "",
    operation_id="createSpace",
    status_code=201,
    dependencies=[Depends(require_json)],
    responses={201: CREATED_LOCATION, **_json_errors},
)
async def create_space(
    body: SpaceName,
    response: Response,
    storage: AuthorizedStorage,
    owner_id: OwnerDependency,
) -> SpaceResponse:
    """在认证账号下创建空间。"""
    space = Space(_ids.new_space_id(), owner_id, body.name, time())
    await storage.create_space(space)
    response.headers["Location"] = f"/v1/spaces/{space.id.value}"
    return SpaceResponse.from_space(space)


@router.get("", operation_id="listSpaces")
async def list_spaces(
    storage: AuthorizedStorage,
    query: Annotated[PageQuery, Query()],
) -> SpacePage:
    """分页列出当前账号的空间。"""
    limit = query.limit
    cursor = SpacePageCursor.decode(query.cursor) if query.cursor is not None else None
    spaces = await storage.list_spaces(
        after_id=cursor.space_id if cursor is not None else "", limit=limit + 1
    )
    return SpacePage(
        items=[SpaceResponse.from_space(space) for space in spaces[:limit]],
        next_cursor=(
            SpacePageCursor(space_id=spaces[limit - 1].id.value).encode()
            if len(spaces) > limit
            else None
        ),
    )


@router.get("/{space_id}", operation_id="getSpace", responses=problem_responses(404))
async def get_space(space_id: ResourceId, storage: AuthorizedStorage) -> SpaceResponse:
    """读取所属空间的元数据。"""
    async with storage.session(SpaceId(space_id)) as reader:
        return SpaceResponse.from_space(await reader.get_space())


@router.patch(
    "/{space_id}",
    operation_id="renameSpace",
    dependencies=[Depends(require_json)],
    responses={**_json_errors, **problem_responses(404)},
)
async def rename_space(
    space_id: ResourceId, body: SpaceName, storage: AuthorizedStorage
) -> SpaceResponse:
    """为所属空间改名。"""
    return SpaceResponse.from_space(
        await storage.rename_space(SpaceId(space_id), body.name)
    )
