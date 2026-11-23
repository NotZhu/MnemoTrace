"""提供事实比较和生命周期测试使用的合成输入。"""

from itertools import count

from mnemotrace.application.extraction import (
    EntityMention,
    ExtractedFact,
    SpaceUserMention,
    TimeEvidence,
)
from mnemotrace.domain.ids import FactId, MessageId, SpaceId
from mnemotrace.domain.values import LiteralValue, Predicate

from ..ports.fakes import FakeIdGenerator

SPACE = SpaceId("adjudication")
_USER = SpaceUserMention()
_LANGUAGE = LiteralValue("英文")


class FactIds(FakeIdGenerator):
    """为测试批次按输入顺序分配不同的事实标识。"""

    def __init__(self) -> None:
        self._ids = count()

    def new_fact_id(self) -> FactId:
        return FactId(f"new-{next(self._ids)}")


def candidate(
    message_id: str,
    *,
    text: str = "用户默认使用英文",
    predicate: str = "default_language",
    subject: EntityMention | SpaceUserMention = _USER,
    target: EntityMention | SpaceUserMention | LiteralValue = _LANGUAGE,
    start: float | None = 10.0,
    end: float | None = None,
    other_evidence: tuple[MessageId, ...] = (),
) -> ExtractedFact:
    """构造带一组共同证据的抽取结果。"""
    source = MessageId(message_id)
    return ExtractedFact(
        subject=subject,
        predicate=Predicate(predicate),
        object=target,
        text=text,
        valid_at_evidence=TimeEvidence("message_time", source, start)
        if start is not None
        else None,
        invalid_at_evidence=TimeEvidence("absolute", source, end)
        if end is not None
        else None,
        provenance=(source, *other_evidence),
        labels=(),
        sensitive=False,
    )
