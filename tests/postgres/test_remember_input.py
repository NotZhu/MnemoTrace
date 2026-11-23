"""验证写入输入边界及 PostgreSQL 前文快照。"""

import asyncio
from dataclasses import replace
from itertools import count
from typing import Literal, cast

import pytest

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.application.remember import MessageInput, _prepare_remember_context
from mnemotrace.domain.enums import Role
from mnemotrace.domain.errors import InvalidMessage
from mnemotrace.domain.ids import MessageId, SessionId
from mnemotrace.ports.storage import SessionUnavailable

from ..ports.fakes import FakeClock, FakeIdGenerator, FakeStorage
from .conftest import PgDatabase
from .sample import SESSION, message, pool, run, space


class _Ids(FakeIdGenerator):
    def __init__(self) -> None:
        self._messages = count()

    def new_message_id(self) -> MessageId:
        return MessageId(f"pending-{next(self._messages)}")


@pytest.mark.parametrize(
    "messages",
    [
        (),
        " \n ",
        "正文\x00",
        "\ud800",
        (MessageInput("assistant", "原文", float("inf")),),
        (MessageInput("user", "原文", 10**12),),
        (MessageInput("user", "原文", False),),
        (MessageInput(cast(Literal["user", "assistant"], "system"), "原文"),),
    ],
    ids=[
        "empty",
        "blank",
        "nul",
        "surrogate",
        "nonfinite",
        "out-of-range",
        "boolean-time",
        "role",
    ],
)
def test_invalid_remember_input_is_rejected(
    messages: str | tuple[MessageInput, ...],
) -> None:
    """在读取会话之前拒绝无效的消息批次。"""

    async def exercise() -> None:
        storage = FakeStorage()
        with pytest.raises(InvalidMessage):
            await _prepare_remember_context(
                storage, space().id, SESSION, messages, clock=FakeClock(), ids=_Ids()
            )

    asyncio.run(exercise())


@pytest.mark.postgres
def test_remember_input_uses_authorized_session_snapshot(database: PgDatabase) -> None:
    """窗口按提交顺序取十条，新消息时间固定且输入准备无写入。"""

    async def exercise() -> None:
        async with pool(database.runtime, max_size=1) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            outsider = PostgresStorage(connections, owner_id="outsider")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            other = SessionId("other")
            history = tuple(
                message(scope, f"history-{i}", at=float(20 - i)) for i in range(12)
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(SESSION)
                await writer.register_session(other)
                await writer.append_messages(
                    (*history, message(scope, "other", session=other))
                )

            inputs = (
                MessageInput("assistant", " 建议从明天开始。 "),
                MessageInput("user", "可以。", 81.0),
            )
            clock, ids = FakeClock(), _Ids()
            ready = await _prepare_remember_context(
                storage, scope.id, SESSION, inputs, clock=clock, ids=ids
            )
            assert ready.received_at == 100.0
            assert ready.erasure_token == "0"
            assert ready.previous == history[-10:]
            first, second = ready.messages
            assert first.id != second.id
            assert first.content == inputs[0].content
            assert first.role == Role.ASSISTANT and second.role == Role.USER
            assert (first.event_time, second.event_time) == (100.0, 81.0)
            assert first.recorded_at == second.recorded_at == ready.received_at

            later = replace(history[-1], id=MessageId("later"))
            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_messages((first.id, second.id)) == {}
                await writer.append_messages((later,))
            assert ready.previous == history[-10:]

            next_input = await _prepare_remember_context(
                storage, scope.id, SESSION, "下一条", clock=clock, ids=ids
            )
            assert next_input.previous == (*history[-9:], later)
            assert next_input.messages[0].role == Role.USER
            with pytest.raises(LookupError, match="空间不可访问"):
                await _prepare_remember_context(
                    outsider, scope.id, SESSION, "下一条", clock=clock, ids=ids
                )
            with pytest.raises(SessionUnavailable):
                await _prepare_remember_context(
                    storage, sibling.id, SESSION, "下一条", clock=clock, ids=ids
                )
            async with storage.session(scope.id, write=True) as writer:
                await writer.delete_session_data(SESSION)
            with pytest.raises(SessionUnavailable):
                await _prepare_remember_context(
                    storage, scope.id, SESSION, "下一条", clock=clock, ids=ids
                )
            other_input = await _prepare_remember_context(
                storage, scope.id, other, "下一条", clock=clock, ids=ids
            )
            assert other_input.erasure_token != ready.erasure_token

    run(exercise())
