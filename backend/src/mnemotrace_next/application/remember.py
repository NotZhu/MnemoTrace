"""记忆写入用例，负责验证输入并固定会话快照。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, tzinfo
from typing import Literal

from ..domain.enums import Role
from ..domain.errors import InvalidMessage
from ..domain.ids import SessionId, SpaceId
from ..domain.message import Message
from ..ports.runtime import Clock, IdGenerator
from ..ports.storage import SessionUnavailable, Storage
from .extraction import message_reference_time

_CONTEXT_MESSAGES = 10


@dataclass(frozen=True)
class MessageInput:
    """接收批次中的一条原文，event_time 是以 UTC epoch 秒表示的发言时刻。"""

    role: Literal["user", "assistant"]
    content: str
    event_time: float | None = None


@dataclass(frozen=True)
class _RememberContext:
    """保存本次使用的已提交前文、新原文和提交前须复核的擦除标记。"""

    received_at: float
    erasure_token: str
    previous: tuple[Message, ...]
    messages: tuple[Message, ...]


async def _prepare_remember_context(
    storage: Storage,
    space_id: SpaceId,
    session_id: SessionId,
    messages: str | Sequence[MessageInput],
    *,
    clock: Clock,
    ids: IdGenerator,
    timezone: tzinfo = UTC,
) -> _RememberContext:
    """校验新消息，在一个只读快照里授权并读取抽取前文。

    Args:
        storage: 已绑定认证账号的存储端口。
        space_id: 目标空间。
        session_id: 已登记的会话。
        messages: 单条用户文本或按提交顺序排列的消息批次。
        clock: 提供本次请求接收时刻。
        ids: 提供本批尚未入库的原文标识。
        timezone: 抽取解释日历日期所用的时区。

    Returns:
        供后续抽取和提交复核复用的输入。

    Raises:
        InvalidMessage: 批次或其中的消息无效。
        LookupError: 无权访问空间，或会话在该空间内不可用。
    """
    received_at = clock.now()
    inputs = (
        (MessageInput("user", messages),) if isinstance(messages, str) else messages
    )
    if not isinstance(inputs, Sequence) or not inputs:
        raise InvalidMessage("消息批次须为非空的有序序列")

    prepared = tuple(
        _prepare_message(item, space_id, session_id, received_at, ids)
        for item in inputs
    )
    if any(
        message_reference_time(message.event_time, timezone=timezone) is None
        for message in prepared
    ):
        raise InvalidMessage("消息发言时刻超出支持的日历范围")
    async with storage.session(space_id) as reader:
        if not await reader.has_session(session_id):
            raise SessionUnavailable("会话不可用")
        erasure_token = await reader.get_erasure_token()
        previous = tuple(await reader.last_messages(session_id, _CONTEXT_MESSAGES))
    return _RememberContext(received_at, erasure_token, previous, prepared)


def _prepare_message(
    item: MessageInput,
    space_id: SpaceId,
    session_id: SessionId,
    received_at: float,
    ids: IdGenerator,
) -> Message:
    if not isinstance(item, MessageInput):
        raise InvalidMessage("批次成员须为 MessageInput")
    try:
        role = Role(item.role)
    except ValueError:
        raise InvalidMessage("role 须为 user 或 assistant") from None
    message = Message(
        id=ids.new_message_id(),
        space_id=space_id,
        session_id=session_id,
        role=role,
        content=item.content,
        event_time=received_at if item.event_time is None else item.event_time,
        recorded_at=received_at,
    )
    try:
        message.content.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidMessage("原文须能编码为 UTF-8") from None
    if "\x00" in message.content:
        raise InvalidMessage("原文不能含空字符")
    return message
