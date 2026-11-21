"""提供进程存活和数据库就绪探针。"""

from asyncio import timeout

import psycopg
from fastapi import APIRouter, HTTPException, Request

from .auth import PoolDependency
from .errors import problem_responses

router = APIRouter(tags=["health"])


@router.get("/livez")
async def live() -> dict[str, str]:
    """确认 HTTP 进程可以响应。"""
    return {"status": "ok"}


@router.get("/readyz", responses=problem_responses(503))
async def ready(request: Request, pool: PoolDependency) -> dict[str, str]:
    """确认数据库可访问且迁移账本匹配运行包。"""
    try:
        async with timeout(3), pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT version, checksum FROM mnemotrace_next.schema_migrations"
            )
            applied: dict[str, str] = dict(await cursor.fetchall())
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName) as exc:
        raise HTTPException(503, detail="database_not_ready") from exc
    except (psycopg.Error, TimeoutError) as exc:
        raise HTTPException(503, detail="database_unavailable") from exc
    if applied != request.app.state.migration_checksums:
        raise HTTPException(503, detail="database_not_ready")
    return {"status": "ready"}
