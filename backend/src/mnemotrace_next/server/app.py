"""装配 MnemoTrace 的 FastAPI 应用和 PostgreSQL 连接池。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from ..adapters.inference import open_inference
from ..adapters.postgres.migrate import migration_checksums
from .auth import ConnectionPool
from .errors import install_error_handlers
from .health import router as health_router
from .middleware import RequestBoundary
from .settings import Settings
from .v1 import memories, sessions, spaces


def create_app(settings: Settings) -> FastAPI:
    """创建带版本化路由和进程共享连接池的 ASGI 应用。"""
    pool: ConnectionPool = AsyncConnectionPool(
        settings.postgres_dsn,
        open=False,
        min_size=0,
        max_size=8,
        timeout=3,
        max_waiting=32,
        kwargs={"connect_timeout": 3},
    )

    embed_configured = any(
        key.startswith("MNEMOTRACE_NEXT_EMBED_") for key in settings.inference_env
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            open_inference(settings.inference_env, embed=embed_configured) as inference,
            pool,
        ):
            app.state.embedder = inference.embedder
            yield

    app = FastAPI(title="MnemoTrace", version="1.0.0", lifespan=lifespan)
    app.state.pool = pool
    app.state.embedder = None
    app.state.migration_checksums = migration_checksums()
    install_error_handlers(app)
    app.add_middleware(RequestBoundary, max_body_bytes=settings.max_body_bytes)
    app.include_router(health_router)
    app.include_router(spaces.router, prefix="/v1")
    app.include_router(sessions.router, prefix="/v1")
    app.include_router(memories.router, prefix="/v1")
    return app
