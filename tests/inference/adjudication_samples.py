"""提供事实比较和生命周期测试使用的合成输入。"""

from mnemotrace.application.extraction import (
    EntityMention,
    ExtractedFact,
    SpaceUserMention,
    TimeEvidence,
)
from mnemotrace.domain.ids import MessageId, SpaceId
from mnemotrace.domain.values import LiteralValue, Predicate

SPACE = SpaceId("adjudication")
_USER = SpaceUserMention()
_LANGUAGE = LiteralValue("英文")


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
