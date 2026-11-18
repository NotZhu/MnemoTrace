"""定义带来源及有效区间的事实。"""

from dataclasses import dataclass, field, replace
from typing import Self

from ._validation import (
    optional_timestamp,
    require_nonblank,
    require_tuple,
    require_type,
    timestamp,
)
from .enums import Source
from .errors import InvalidFact
from .ids import FactId, MessageId, SpaceId
from .values import ContentHash, EntityRef, Label, LiteralValue, Predicate, content_hash


@dataclass(frozen=True)
class Fact:
    """保存断言、来源和左闭右开的有效区间。

    有效起点可以未知。expired_at 记录首次获知有效截止的时刻。
    """

    id: FactId
    space_id: SpaceId
    subject: EntityRef
    predicate: Predicate
    object: EntityRef | LiteralValue
    text: str
    valid_at: float | None
    invalid_at: float | None
    recorded_at: float
    expired_at: float | None
    provenance: tuple[MessageId, ...]
    labels: tuple[Label, ...]
    sensitive: bool
    source: Source
    supersedes: tuple[FactId, ...] = ()
    content_hash: ContentHash = field(init=False)

    def __post_init__(self) -> None:
        require_type(self.id, FactId, "id", InvalidFact)
        require_type(self.space_id, SpaceId, "space_id", InvalidFact)
        require_type(self.subject, EntityRef, "subject", InvalidFact)
        require_type(self.predicate, Predicate, "predicate", InvalidFact)
        require_type(self.object, (EntityRef, LiteralValue), "object", InvalidFact)
        require_nonblank(self.text, "text", InvalidFact)
        require_type(self.sensitive, bool, "sensitive", InvalidFact)
        require_type(self.source, Source, "source", InvalidFact)
        require_tuple(self.provenance, MessageId, "provenance", InvalidFact)
        require_tuple(self.labels, Label, "labels", InvalidFact)
        require_tuple(self.supersedes, FactId, "supersedes", InvalidFact)

        if self.source is Source.EXTRACTED and not self.provenance:
            raise InvalidFact("抽取事实必须包含 provenance")
        if self.id in self.supersedes or len(set(self.supersedes)) != len(
            self.supersedes
        ):
            raise InvalidFact("supersedes 必须由互异的直接前驱组成，且不能引用自身")

        valid_at = optional_timestamp(self.valid_at, "valid_at", InvalidFact)
        invalid_at = optional_timestamp(self.invalid_at, "invalid_at", InvalidFact)
        recorded_at = timestamp(self.recorded_at, "recorded_at", InvalidFact)
        expired_at = optional_timestamp(self.expired_at, "expired_at", InvalidFact)
        if (invalid_at is None) != (expired_at is None):
            raise InvalidFact("invalid_at 和 expired_at 必须成对")
        if valid_at is not None and invalid_at is not None and valid_at >= invalid_at:
            raise InvalidFact("valid_at 必须早于 invalid_at")
        if expired_at is not None and expired_at < recorded_at:
            raise InvalidFact("expired_at 必须晚于或等于 recorded_at")

        try:
            fingerprint = ContentHash(content_hash(self.text))
        except UnicodeEncodeError as exc:
            raise InvalidFact("text 必须能编码为 UTF-8") from exc
        object.__setattr__(self, "valid_at", valid_at)
        object.__setattr__(self, "invalid_at", invalid_at)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "expired_at", expired_at)
        object.__setattr__(self, "provenance", tuple(dict.fromkeys(self.provenance)))
        labels = tuple(sorted(set(self.labels), key=lambda label: label.value))
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "content_hash", fingerprint)

    @property
    def is_edge(self) -> bool:
        """判断事实能否作为关系边使用。

        Returns:
            对象为实体引用时为 True。
        """
        return isinstance(self.object, EntityRef)

    def close(self, valid_end: float, closed_at: float) -> Self:
        """首次记录事实的有效截止及获知时刻。

        Args:
            valid_end: 事实停止有效的时刻。
            closed_at: 首次得知有效截止的时刻。

        Returns:
            写入截止的同 ID 事实。

        Raises:
            InvalidFact: 已经设置截止，或传入的时刻违反时间约束。
        """
        if self.invalid_at is not None:
            raise InvalidFact("事实已记录有效截止")
        return replace(
            self,
            invalid_at=timestamp(valid_end, "valid_end", InvalidFact),
            expired_at=timestamp(closed_at, "closed_at", InvalidFact),
        )

    def shorten_validity(self, valid_end: float) -> Self:
        """根据迟到证据提前有效截止，保留首次获知时刻。

        Args:
            valid_end: 更早的有效截止时刻。

        Returns:
            缩短有效区间的同 ID 事实。

        Raises:
            InvalidFact: 缺少原截止，或新截止违反时间约束。
        """
        invalid_at = timestamp(valid_end, "valid_end", InvalidFact)
        if self.invalid_at is None or invalid_at >= self.invalid_at:
            raise InvalidFact("新截止必须早于已记录的有效截止")
        return replace(self, invalid_at=invalid_at)
