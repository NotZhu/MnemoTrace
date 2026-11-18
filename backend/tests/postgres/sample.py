"""提供真库测试使用的合成领域记录。"""

import asyncio
from collections.abc import Coroutine
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Role, Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, MessageId, SessionId, SpaceId
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue, Predicate

SESSION = SessionId("session")


def run(coro: Coroutine[object, object, None]) -> None:
    """使用 psycopg 在各平台均支持的事件循环运行测试。

    Args:
        coro: 待执行的测试协程。
    """
    asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def pool(
    dsn: str, *, max_size: int = 4
) -> AsyncConnectionPool[AsyncConnection[TupleRow]]:
    """创建供测试异步上下文管理的连接池。

    Args:
        dsn: 专用测试库的连接串。
        max_size: 连接池的并发连接上限。

    Returns:
        尚未打开的异步连接池。
    """
    return AsyncConnectionPool(
        dsn, open=False, min_size=0, max_size=max_size, timeout=5
    )


def space(owner: str = "owner") -> Space:
    """构造具有随机标识的测试空间。

    Args:
        owner: 测试空间的所有者标识。

    Returns:
        待写入存储的空间快照。
    """
    return Space(SpaceId(uuid4().hex), owner, "测试空间", 0.0)


def message(
    scope: Space,
    identifier: str,
    *,
    session: SessionId = SESSION,
    at: float = 10.0,
    content: str = "喜欢乌龙茶",
) -> Message:
    """构造一条可原样重放的合成原文。

    Args:
        scope: 原文所属的测试空间。
        identifier: 原文标识。
        session: 原文所属的会话标识。
        at: 同时用作事件和收录时刻的 UTC epoch 秒。
        content: 合成正文。

    Returns:
        待追加或重放的用户原文。
    """
    return Message(MessageId(identifier), scope.id, session, Role.USER, content, at, at)


def fact(subject: Entity, identifier: str, *, text: str = "喜欢乌龙茶") -> Fact:
    """构造一条由测试主体派生的手工事实。

    Args:
        subject: 事实主体，提供所属空间。
        identifier: 事实标识。
        text: 合成事实的正文。

    Returns:
        测试空间内有效起点未知的手工事实。
    """
    return Fact(
        id=FactId(identifier),
        space_id=subject.space_id,
        subject=EntityRef(subject.id),
        predicate=Predicate("喜欢"),
        object=LiteralValue("乌龙茶", "饮品"),
        text=text,
        valid_at=None,
        invalid_at=None,
        recorded_at=10.0,
        expired_at=None,
        provenance=(),
        labels=(),
        sensitive=False,
        source=Source.MANUAL,
    )
