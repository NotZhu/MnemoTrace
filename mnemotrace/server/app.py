"""装配 MnemoTrace 的 FastAPI 应用和 PostgreSQL 连接池。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..adapters.inference import open_inference
from ..adapters.postgres.migrate import migration_checksums
from ..adapters.postgres.pool import runtime_pool
from .errors import install_error_handlers
from .health import router as health_router
from .middleware import RequestBoundary
from .recall_usage import RecallUsageQueue
from .settings import Settings
from .v1 import memories, operations, profiles, sessions, spaces


def create_app(settings: Settings) -> FastAPI:
    """创建带版本化路由和进程共享连接池的 ASGI 应用。"""
    pool = runtime_pool(settings.runtime, max_size=8, max_waiting=32)

    embed_configured = any(
        key.startswith("MNEMOTRACE_EMBED_") for key in settings.runtime.inference_env
    )
    rerank_configured = any(
        key.startswith("MNEMOTRACE_RERANK_") for key in settings.runtime.inference_env
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            open_inference(
                settings.runtime.inference_env,
                llm="optional",
                embed=embed_configured,
                rerank=rerank_configured,
            ) as inference,
            pool,
            RecallUsageQueue().run() as recall_usage,
        ):
            app.state.embedder = inference.embedder
            app.state.reranker = inference.reranker
            app.state.recall_usage = recall_usage
            app.state.write_configured = (
                inference.llm is not None and inference.embedder is not None
            )
            try:
                yield
            finally:
                app.state.recall_usage = None

    app = FastAPI(
        title="MnemoTrace",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.pool = pool
    app.state.maintenance_timeout = settings.runtime.database_timeouts.maintenance
    app.state.embedder = None
    app.state.reranker = None
    app.state.recall_usage = None
    app.state.write_configured = False
    app.state.queue_limit = settings.queue_limit
    app.state.extraction_timezone = settings.runtime.extraction_timezone
    app.state.migration_checksums = migration_checksums()
    install_error_handlers(app)
    app.add_middleware(RequestBoundary, max_body_bytes=settings.max_body_bytes)
    app.include_router(health_router)
    app.include_router(spaces.router, prefix="/v1")
    app.include_router(sessions.router, prefix="/v1")
    app.include_router(memories.router, prefix="/v1")
    app.include_router(operations.router, prefix="/v1")
    app.include_router(profiles.router, prefix="/v1")
    return app
