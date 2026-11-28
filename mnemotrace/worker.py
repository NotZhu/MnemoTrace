"""启动独立的 PostgreSQL 后台任务 worker。"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from types import FrameType
from typing import Protocol

import psycopg

from .adapters.inference import InferenceRequestError, open_inference
from .adapters.postgres.migrate import migration_checksums
from .adapters.postgres.operations import PostgresOperations
from .adapters.postgres.pool import runtime_pool
from .adapters.postgres.profile_refresh import PostgresProfileRefresh
from .adapters.postgres.session_summary_refresh import PostgresSessionSummaryRefresh
from .adapters.postgres.storage import PostgresStorage
from .adapters.runtime import SystemClock, UuidGenerator
from .application.operations import ClaimedOperation, OperationRunner
from .application.profile_refresh import ProfileRefreshRunner
from .application.remember import Remember
from .application.session_summary_refresh import SessionSummaryRunner
from .runtime_settings import RuntimeSettings

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WorkerConcurrency:
    """记录当前 worker 的写入、画像和摘要消费者数量。"""

    write: int = 2
    profile: int = 1
    summary: int = 1


class _Runner(Protocol):
    async def run_once(self) -> bool:
        """领取或结算一项任务时返回 True。"""
        ...


def _is_transient(error: Exception) -> bool:
    if isinstance(error, InferenceRequestError):
        return error.kind in ("rate_limit", "upstream", "connection")
    return isinstance(error, psycopg.OperationalError)


async def _consume(runner: _Runner, stopping: asyncio.Event) -> None:
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
    concurrency: _WorkerConcurrency,
    stopping: asyncio.Event,
    readiness_port: int | None,
) -> None:
    max_db_connections = concurrency.write + concurrency.profile + concurrency.summary
    pool = runtime_pool(settings, max_size=max_db_connections)
    async with (
        open_inference(settings.inference_env, llm=True, embed=True) as clients,
        pool,
    ):
        llm, embedder = clients.llm, clients.embedder
        assert llm is not None and embedder is not None
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT version, checksum FROM mnemotrace.schema_migrations"
            )
            if dict(await cursor.fetchall()) != migration_checksums():
                raise ValueError("请先应用当前版本的 PostgreSQL 迁移")

        def storage_for(owner: str) -> PostgresStorage:
            return PostgresStorage(pool, owner_id=owner)

        def remember_for(job: ClaimedOperation) -> Remember:
            return Remember(
                space_id=job.space_id,
                storage=storage_for(job.owner_id),
                llm=llm,
                embedder=embedder,
                clock=SystemClock(),
                ids=UuidGenerator(),
                timezone=settings.extraction_timezone,
            )

        write_runner = OperationRunner(
            PostgresOperations(pool), remember_for, _is_transient
        )
        profile_runner = ProfileRefreshRunner(
            PostgresProfileRefresh(pool),
            storage_for,
            llm,
            SystemClock(),
            _is_transient,
        )
        summary_runner = SessionSummaryRunner(
            PostgresSessionSummaryRefresh(pool), storage_for, llm, _is_transient
        )
        readiness = (
            await asyncio.start_server(_close_probe, "127.0.0.1", readiness_port)
            if readiness_port is not None
            else nullcontext()
        )
        _LOG.info(
            "后台 worker 已启动 write_concurrency=%d profile_concurrency=%d "
            "summary_concurrency=%d max_db_connections=%d",
            concurrency.write,
            concurrency.profile,
            concurrency.summary,
            max_db_connections,
        )
        async with asyncio.TaskGroup() as workers, readiness:
            for runner, count in (
                (write_runner, concurrency.write),
                (profile_runner, concurrency.profile),
                (summary_runner, concurrency.summary),
            ):
                for _ in range(count):
                    workers.create_task(_consume(runner, stopping))
            await stopping.wait()


async def _run(
    settings: RuntimeSettings,
    concurrency: _WorkerConcurrency,
    readiness_port: int | None = None,
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
        await _serve(settings, concurrency, stopping, readiness_port)
    except asyncio.CancelledError:
        if not stopping.is_set():
            raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> int:
    """读取运行和并发配置，启动后台任务消费者。"""
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = _WorkerConcurrency()
    for option, default, description in (
        ("--concurrency", defaults.write, "记忆写入消费者数量"),
        ("--profile-concurrency", defaults.profile, "画像刷新消费者数量"),
        ("--summary-concurrency", defaults.summary, "会话摘要消费者数量"),
    ):
        parser.add_argument(
            option,
            type=int,
            choices=range(1, 17),
            default=default,
            metavar="1-16",
            help=description,
        )
    parser.add_argument("--readiness-port", type=int, help="在本机监听的就绪探针端口")
    args = parser.parse_args()
    if args.readiness_port is not None and not 1 <= args.readiness_port <= 65535:
        parser.error("--readiness-port 须为 1 至 65535")
    concurrency = _WorkerConcurrency(
        write=args.concurrency,
        profile=args.profile_concurrency,
        summary=args.summary_concurrency,
    )
    logging.basicConfig(level=logging.WARNING)
    _LOG.setLevel(logging.INFO)
    try:
        settings = RuntimeSettings.from_env(os.environ)
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        asyncio.run(
            _run(settings, concurrency, args.readiness_port),
            loop_factory=loop_factory,
        )
    except ValueError as exc:
        parser.error(str(exc))
    except (psycopg.Error, OSError, ExceptionGroup) as exc:
        _LOG.error("后台 worker 中止 type=%s", type(exc).__name__)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
