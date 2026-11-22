"""启动独立的 PostgreSQL 记忆写入 worker。"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import nullcontext
from datetime import tzinfo
from types import FrameType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from .adapters.inference import InferenceRequestError, open_inference
from .adapters.postgres.migrate import migration_checksums
from .adapters.postgres.operations import PostgresOperations
from .adapters.postgres.profile_refresh import PostgresProfileRefresh
from .adapters.postgres.storage import PostgresStorage
from .adapters.runtime import SystemClock, UuidGenerator
from .application.operations import ClaimedOperation, OperationRunner
from .application.profile_refresh import ProfileRefreshRunner
from .application.remember import Remember
from .runtime_settings import RuntimeSettings

_LOG = logging.getLogger(__name__)


def _is_transient(error: Exception) -> bool:
    if isinstance(error, InferenceRequestError):
        return error.kind in ("rate_limit", "upstream", "connection")
    return isinstance(error, psycopg.OperationalError)


async def _consume(
    runner: OperationRunner | ProfileRefreshRunner, stopping: asyncio.Event
) -> None:
    while not stopping.is_set():
        try:
            if await runner.run_once():
                continue
        except psycopg.OperationalError as exc:
            _LOG.warning("任务队列暂不可用 sqlstate=%s", exc.sqlstate)
        await asyncio.sleep(1)


def _close_probe(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


async def _serve(
    settings: RuntimeSettings,
    concurrency: int,
    stopping: asyncio.Event,
    readiness_port: int | None,
    *,
    extraction_timezone: tzinfo,
) -> None:
    pool: AsyncConnectionPool[AsyncConnection[TupleRow]] = AsyncConnectionPool(
        settings.postgres_dsn,
        open=False,
        min_size=0,
        max_size=concurrency + 1,
        timeout=3,
        kwargs={"connect_timeout": 3},
    )
    async with (
        open_inference(settings.inference_env, llm=True, embed=True) as clients,
        pool,
    ):
        llm, embedder = clients.llm, clients.embedder
        assert llm is not None and embedder is not None
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT version, checksum FROM mnemotrace_next.schema_migrations"
            )
            if dict(await cursor.fetchall()) != migration_checksums():
                raise ValueError("请先应用当前版本的 PostgreSQL 迁移")

        def remember_for(job: ClaimedOperation) -> Remember:
            return Remember(
                space_id=job.space_id,
                storage=PostgresStorage(pool, owner_id=job.owner_id),
                llm=llm,
                embedder=embedder,
                clock=SystemClock(),
                ids=UuidGenerator(),
                timezone=extraction_timezone,
            )

        runner = OperationRunner(PostgresOperations(pool), remember_for, _is_transient)
        profile_runner = ProfileRefreshRunner(
            PostgresProfileRefresh(pool),
            lambda owner: PostgresStorage(pool, owner_id=owner),
            llm,
            SystemClock(),
            _is_transient,
        )
        readiness = (
            await asyncio.start_server(_close_probe, "127.0.0.1", readiness_port)
            if readiness_port is not None
            else nullcontext()
        )
        _LOG.info("写入 worker 已启动 concurrency=%d", concurrency)
        async with asyncio.TaskGroup() as workers, readiness:
            for _ in range(concurrency):
                workers.create_task(_consume(runner, stopping))
            workers.create_task(_consume(profile_runner, stopping))
            await stopping.wait()


async def _run(
    settings: RuntimeSettings,
    concurrency: int,
    readiness_port: int | None = None,
    *,
    extraction_timezone: tzinfo,
) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    supervisor = asyncio.current_task()
    assert supervisor is not None

    def stop() -> None:
        if not stopping.is_set():
            # 归还已提交事务的连接时可能消耗取消，领取循环仍须观察到停机。
            stopping.set()
            supervisor.cancel()

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop)

    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    try:
        for signum in signals:
            signal.signal(signum, on_signal)
        await _serve(
            settings,
            concurrency,
            stopping,
            readiness_port,
            extraction_timezone=extraction_timezone,
        )
    except asyncio.CancelledError:
        if not stopping.is_set():
            raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> int:
    """读取数据库、推理和抽取时区配置并运行写入任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=2, metavar="1-16")
    parser.add_argument("--readiness-port", type=int, help="在本机监听的就绪探针端口")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16:
        parser.error("--concurrency 须为 1 至 16")
    if args.readiness_port is not None and not 1 <= args.readiness_port <= 65535:
        parser.error("--readiness-port 须为 1 至 65535")
    try:
        extraction_timezone = ZoneInfo(
            os.environ.get("MNEMOTRACE_NEXT_EXTRACTION_TIMEZONE", "UTC")
        )
    except (ZoneInfoNotFoundError, ValueError):
        parser.error("MNEMOTRACE_NEXT_EXTRACTION_TIMEZONE 须为有效的 IANA 时区名称")
    logging.basicConfig(level=logging.WARNING)
    _LOG.setLevel(logging.INFO)
    try:
        settings = RuntimeSettings.from_env(os.environ)
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        asyncio.run(
            _run(
                settings,
                args.concurrency,
                args.readiness_port,
                extraction_timezone=extraction_timezone,
            ),
            loop_factory=loop_factory,
        )
    except ValueError as exc:
        parser.error(str(exc))
    except (psycopg.Error, OSError, ExceptionGroup) as exc:
        _LOG.error("写入 worker 中止 type=%s", type(exc).__name__)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
