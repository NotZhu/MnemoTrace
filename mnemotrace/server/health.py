"""提供进程存活和数据库就绪探针。"""

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from ..adapters.postgres.migrate import MigrationReadiness
from .errors import problem_responses

router = APIRouter(tags=["health"])

type ReadinessCheck = Callable[[], Awaitable[MigrationReadiness]]


async def readiness_check(request: Request) -> ReadinessCheck:
    """取得应用装配的数据库就绪检查。"""
    return cast(ReadinessCheck, request.app.state.check_readiness)


@router.get("/livez")
async def live() -> dict[str, str]:
    """确认 HTTP 进程可以响应。"""
    return {"status": "ok"}


@router.get("/readyz", responses=problem_responses(503))
async def ready(
    check: Annotated[ReadinessCheck, Depends(readiness_check)],
) -> dict[str, str]:
    """确认数据库可访问且迁移账本匹配运行包。"""
    result = await check()
    if result == "not_ready":
        raise HTTPException(503, detail="database_not_ready")
    if result == "unavailable":
        raise HTTPException(503, detail="database_unavailable")
    return {"status": "ready"}
