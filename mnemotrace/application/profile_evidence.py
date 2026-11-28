"""判断事实能否用于画像以及变更对画像分组的影响。"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from ..domain.entity import Entity
from ..domain.enums import ProfileGroup, Source
from ..domain.fact import Fact
from ..domain.ids import EntityId


@dataclass(frozen=True)
class ProfileFactState:
    """保存画像判断所需的事实字段。"""

    subject_id: EntityId
    source: Source
    sensitive: bool
    groups: tuple[ProfileGroup, ...]
    valid_at: float | None
    invalid_at: float | None

    @classmethod
    def from_fact(cls, fact: Fact) -> Self:
        """选择完整事实中与画像相关的字段。"""
        return cls(
            fact.subject.entity_id,
            fact.source,
            fact.sensitive,
            fact.profile_groups,
            fact.valid_at,
            fact.invalid_at,
        )

    def evidence_for(self, subject_id: EntityId) -> Self | None:
        """返回符合主体、来源、分组和敏感性条件的证据。"""
        if (
            self.subject_id == subject_id
            and self.source is Source.EXTRACTED
            and self.groups
            and not self.sensitive
        ):
            return self
        return None

    def is_current(self, at: float) -> bool:
        """判断给定时点是否位于左闭右开的有效期。"""
        return (self.valid_at is None or self.valid_at <= at) and (
            self.invalid_at is None or at < self.invalid_at
        )


type ProfileFactChange = tuple[ProfileFactState | None, ProfileFactState | None]


@dataclass
class ProfileEvidenceImpact:
    """记录立即受影响的分组、待核对的截止和最早未来边界。"""

    dirty: set[ProfileGroup]
    expiries: list[tuple[frozenset[ProfileGroup], float]]
    transitions: dict[ProfileGroup, float]


def is_profile_evidence(fact: Fact, subject: Entity, at: float) -> bool:
    """判断事实在指定时刻能否作为空间用户的画像证据。"""
    state = ProfileFactState.from_fact(fact)
    return (
        fact.space_id == subject.space_id
        and state.evidence_for(subject.id) is not None
        and state.is_current(at)
    )


def changed_profile_evidence(
    changes: Iterable[ProfileFactChange], subject_id: EntityId
) -> list[ProfileFactChange]:
    """筛出实际改变的画像证据，便于调用方按需取得判断时点。"""
    evidence_changes = []
    for before, after in changes:
        previous = before.evidence_for(subject_id) if before is not None else None
        current = after.evidence_for(subject_id) if after is not None else None
        if previous != current:
            evidence_changes.append((previous, current))
    return evidence_changes


def profile_evidence_impact(
    changes: Iterable[ProfileFactChange], at: float
) -> ProfileEvidenceImpact:
    """计算已筛选的证据变更对当前及未来分组的影响。"""
    impact = ProfileEvidenceImpact(set(), [], {})
    for before, after in changes:
        for state in (before, after):
            if state is not None and state.is_current(at):
                impact.dirty.update(state.groups)

        # 证据将被撤下或改变截止。若旧截止尚未处理，相关组仍需刷新。
        if (
            before is not None
            and before.invalid_at is not None
            and before.invalid_at <= at
            and (
                after is None
                or after.invalid_at != before.invalid_at
                or any(group not in after.groups for group in before.groups)
            )
        ):
            impact.expiries.append((frozenset(before.groups), before.invalid_at))

        if after is not None:
            boundary = min(
                (
                    t
                    for t in (after.valid_at, after.invalid_at)
                    if t is not None and t > at
                ),
                default=None,
            )
            if boundary is not None:
                for group in after.groups:
                    impact.transitions[group] = min(
                        boundary, impact.transitions.get(group, boundary)
                    )
    return impact
