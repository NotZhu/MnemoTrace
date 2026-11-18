"""验证原文记录和消息时间。"""

from dataclasses import FrozenInstanceError, replace

import pytest

from mnemotrace_next.domain.enums import Role
from mnemotrace_next.domain.errors import InvalidMessage
from mnemotrace_next.domain.ids import MessageId, SessionId, SpaceId
from mnemotrace_next.domain.message import Message


@pytest.fixture
def message() -> Message:
    """构造用于原文测试的用户消息。

    Returns:
        含首尾空白且事件时刻未知的原文快照。
    """
    return Message(
        MessageId("message"),
        SpaceId("space"),
        SessionId("session"),
        Role.USER,
        " 茶 ",
        None,
        3,
    )


def test_message_keeps_original_text_and_optional_time(message: Message) -> None:
    assert message.content == " 茶 "
    assert message.event_time is None
    assert type(message.recorded_at) is float

    timed = replace(message, event_time=0, role=Role.ASSISTANT)
    assert timed.event_time == 0.0
    assert type(timed.event_time) is float
    assert timed.role.value == "assistant"
    with pytest.raises(FrozenInstanceError):
        message.content = "其他"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    (
        {"content": " \t"},
        {"id": "message"},
        {"space_id": SessionId("space")},
        {"session_id": ""},
        {"role": "user"},
        {"role": "system"},
        {"event_time": float("nan")},
        {"recorded_at": None},
    ),
)
def test_message_rejects_invalid_fields(
    message: Message, changes: dict[str, object]
) -> None:
    with pytest.raises(InvalidMessage):
        replace(message, **changes)  # type: ignore[arg-type]
