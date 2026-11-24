"""认证账号级 Bearer 凭据并装配受限存储。"""

from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..adapters.postgres import PostgresStorage
from ..adapters.postgres.identity import authenticate
from ..adapters.postgres.pool import ConnectionPool

_bearer = HTTPBearer(auto_error=False, scheme_name="ApiKey")


def connection_pool(request: Request) -> ConnectionPool:
    """取得由进程生命周期管理的数据库连接池。"""
    return cast(ConnectionPool, request.app.state.pool)


PoolDependency = Annotated[ConnectionPool, Depends(connection_pool)]


async def authenticated_owner(
    pool: PoolDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> str:
    """从 Bearer 凭据取得服务端记录的账号。"""
    owner_id = (
        await authenticate(pool, credentials.credentials)
        if credentials is not None
        else None
    )
    if owner_id is None:
        raise HTTPException(
            401, detail="invalid_api_key", headers={"WWW-Authenticate": "Bearer"}
        )
    return owner_id


OwnerDependency = Annotated[str, Depends(authenticated_owner)]


async def authorized_storage(
    pool: PoolDependency, owner_id: OwnerDependency, request: Request
) -> PostgresStorage:
    """为已认证账号绑定空间存储。"""
    return PostgresStorage(
        pool,
        owner_id=owner_id,
        maintenance_timeout=request.app.state.maintenance_timeout,
    )


AuthorizedStorage = Annotated[PostgresStorage, Depends(authorized_storage)]
