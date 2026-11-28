"""在 HTTP 进程内异步强化最终召回证据。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from ..application.recall import RecallUsage
from ..application.salience import reinforce_context_facts
from ..ports.runtime import Clock
from ..ports.storage import Storage

_LOG = logging.getLogger(__name__)


class RecallUsageQueue:
    """每个服务进程持有一个有界的尽力写回队列。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._pending: asyncio.Queue[tuple[Storage, RecallUsage]] = asyncio.Queue(
            maxsize=128
        )
        self._accepting = False

    @asynccontextmanager
    async def run(self) -> AsyncIterator["RecallUsageQueue"]:
        """在应用生命周期内接收反馈，停机时结束消费者。"""
        self._accepting = True
        worker = asyncio.create_task(self._consume())
        try:
            yield self
        finally:
            self._accepting = False
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    def submit(self, storage: Storage, usage: RecallUsage) -> None:
        """登记已确定的响应证据，队列满或关闭时允许丢弃。"""
        if not self._accepting:
            return
        try:
            self._pending.put_nowait((storage, usage))
        except asyncio.QueueFull:
            _LOG.debug("召回强化队列已满，丢弃反馈")

    async def _consume(self) -> None:
        while self._accepting:
            storage, usage = await self._pending.get()
            try:
                await reinforce_context_facts(
                    storage,
                    self._clock,
                    usage.space_id,
                    usage.fact_ids,
                    reference_time=usage.reference_time,
                    expected_erasure_token=usage.erasure_token,
                    exclude_sensitive=usage.exclude_sensitive,
                    require_memory_decay=True,
                )
            except Exception as exc:  # noqa: BLE001 - 单次写失败不能中止后续反馈。
                _LOG.warning("召回强化写入失败 reason=%s", type(exc).__name__)
            finally:
                self._pending.task_done()
