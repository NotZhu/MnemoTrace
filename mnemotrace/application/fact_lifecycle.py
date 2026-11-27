"""计算事实归并、有效区间与截止归因。"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ..domain.entity import Entity
from ..domain.fact import Fact, intervals_disjoint
from ..domain.ids import EntityId, FactId
from ..ports.storage import CutoffTrigger, SupportGroups, flatten_support_groups
from .adjudication import FactComparison, FactPair, PreparedFact, Verdict


@dataclass(frozen=True)
class FactWrites:
    """保存供写入用例提交的事实和实际需要的依赖。"""

    facts: tuple[Fact, ...]
    support_groups: Mapping[FactId, SupportGroups]
    cutoff_triggers: Mapping[FactId, tuple[CutoffTrigger, ...]]
    entities: tuple[Entity, ...]
    vectors: Mapping[FactId, tuple[float, ...]]


@dataclass(frozen=True)
class _Cutoff:
    earlier_id: FactId
    later_id: FactId
    compared_trigger_fact_ids: tuple[FactId, ...]
    earlier_valid_at: float
    valid_end: float


def calculate_fact_writes(
    prepared: Sequence[PreparedFact],
    comparisons: Sequence[FactComparison],
    *,
    existing_facts: Mapping[FactId, Fact],
    existing_support_groups: Mapping[FactId, SupportGroups],
    changed_at: float,
) -> FactWrites:
    """根据固定裁决和最新证据组计算待保存的事实快照。

    Args:
        prepared: 按输入顺序排列的候选事实。
        comparisons: 按原配对顺序排列的比较结果。
        existing_facts: 构造比较时使用的旧事实快照。
        existing_support_groups: 同一写会话中重复或矛盾裁决涉及的旧事实证据组。
            无来源的事实对应空元组。
        changed_at: 本次审计变更时刻。首次获知截止的时刻另受各事实录入时间约束。

    Returns:
        每个受影响事实的一份最终快照及其写入依赖。
    """
    # 根据裁决为重复事实选代表，无法确定的候选暂缓成为新事实。
    incoming = {item.fact.id: item for item in prepared}
    facts = {
        **existing_facts,
        **{fact_id: item.fact for fact_id, item in incoming.items()},
    }
    duplicates, suspended = _collect_decisions(comparisons, facts, incoming)
    targets, new_ids = _select_representatives(
        prepared, existing_facts, duplicates, suspended
    )

    # 用冲突裁决确定事实的截止和触发依据，标记仍有争议的新代表。
    closed, cutoff_triggers, uncertain_representatives = _resolve_cutoffs(
        comparisons,
        facts,
        incoming,
        targets,
        existing_facts,
        existing_support_groups,
        suspended,
        changed_at,
    )
    new_ids.difference_update(uncertain_representatives)

    # 为可写入的新事实组装最终快照及所需的证据、实体和向量。
    updated: dict[FactId, Fact] = {}
    support_groups: dict[FactId, SupportGroups] = {}
    entities: dict[EntityId, Entity] = {}
    vectors: dict[FactId, tuple[float, ...]] = {}
    for item in prepared:
        fact = item.fact
        if fact.id in new_ids:
            snapshot = closed.pop(fact.id, fact)
            updated[fact.id] = (
                replace(snapshot, expired_at=max(changed_at, snapshot.recorded_at))
                if snapshot.invalid_at is not None
                else snapshot
            )
            support_groups[fact.id] = (fact.provenance,)
            vectors[fact.id] = item.vector
            for entity in item.entities:
                entities.setdefault(entity.id, entity)

    # 截止裁决可能缩短代表事实的有效期，重复证据须按最终区间验收。
    updated.update(closed)
    for source, target in targets.items():
        if source == target or target in uncertain_representatives:
            continue
        final_fact = updated.get(target, facts[target])
        if not _covers_interval(incoming[source], final_fact):
            continue
        _append_support(
            final_fact,
            incoming[source].fact,
            existing_support_groups,
            updated,
            support_groups,
        )

    return FactWrites(
        tuple(updated.values()),
        support_groups,
        cutoff_triggers,
        tuple(entities.values()),
        vectors,
    )


def _collect_decisions(
    comparisons: Sequence[FactComparison],
    facts: Mapping[FactId, Fact],
    incoming: Mapping[FactId, PreparedFact],
) -> tuple[dict[FactId, set[FactId]], set[FactId]]:
    duplicates: dict[FactId, set[FactId]] = defaultdict(set)
    suspended: set[FactId] = set()
    for comparison in comparisons:
        left, right = comparison.pair
        if intervals_disjoint(facts[left], facts[right]):
            continue
        if comparison.verdict == "duplicate":
            duplicates[left].add(right)
            duplicates[right].add(left)
        elif comparison.verdict == "unknown" or (
            comparison.verdict == "conflict"
            and _order_by_start_time(facts[left], facts[right]) is None
        ):
            suspended.update(incoming.keys() & {left, right})
    return duplicates, suspended


def _select_representatives(
    prepared: Sequence[PreparedFact],
    existing: Mapping[FactId, Fact],
    duplicates: Mapping[FactId, set[FactId]],
    suspended: set[FactId],
) -> tuple[dict[FactId, FactId], set[FactId]]:
    targets: dict[FactId, FactId] = {}
    retained: dict[FactId, int] = {}
    for index, item in enumerate(prepared):
        source = item.fact.id
        direct = duplicates.get(source, set())
        old_targets = existing.keys() & direct
        if old_targets:
            targets[source] = min(
                old_targets,
                key=lambda fact_id: (existing[fact_id].recorded_at, fact_id.value),
            )
        elif source not in suspended:
            new_targets = retained.keys() & direct
            representative = min(new_targets, key=retained.__getitem__, default=source)
            targets[source] = representative
            if representative == source:
                retained[source] = index
    return targets, set(retained)


def _covers_interval(item: PreparedFact, target: Fact) -> bool:
    fact = item.fact
    if item.valid_at_evidence is not None and fact.valid_at is None:
        return False
    if target.valid_at is not None and (
        fact.valid_at is None or fact.valid_at > target.valid_at
    ):
        return False
    if target.invalid_at is None:
        return fact.invalid_at is None and item.invalid_at_evidence is None
    return fact.invalid_at is not None and fact.invalid_at >= target.invalid_at


def _order_by_start_time(left: Fact, right: Fact) -> tuple[Fact, Fact] | None:
    if (
        left.valid_at is None
        or right.valid_at is None
        or left.valid_at == right.valid_at
    ):
        return None
    return (left, right) if left.valid_at < right.valid_at else (right, left)


def _resolve_cutoffs(
    comparisons: Sequence[FactComparison],
    facts: Mapping[FactId, Fact],
    incoming: Mapping[FactId, PreparedFact],
    targets: Mapping[FactId, FactId],
    existing: Mapping[FactId, Fact],
    groups: Mapping[FactId, SupportGroups],
    suspended: set[FactId],
    changed_at: float,
) -> tuple[dict[FactId, Fact], dict[FactId, tuple[CutoffTrigger, ...]], set[FactId]]:
    new_ids = {source for source, target in targets.items() if source == target}
    projectable: dict[FactId, FactId] = {}
    deferred: dict[FactId, FactId] = {}
    for source, target in targets.items():
        if source == target or _covers_interval(incoming[source], facts[target]):
            projectable[source] = target
        elif source not in suspended and incoming[source].fact.invalid_at is not None:
            deferred[source] = target

    # 有截止的重复证据先等代表通过独立裁决缩短，避免证据用自己的冲突证明覆盖。
    eligible: dict[FactId, FactId] = {}
    uncertain: set[FactId] = set()
    while True:
        cutoffs, disputed = _project_conflicts(
            comparisons,
            facts,
            existing,
            new_ids - uncertain,
            {
                source: target
                for source, target in (projectable | eligible).items()
                if target not in uncertain
            },
            suspended,
        )
        if disputed:
            uncertain.update(disputed)
            # 暂缓的事实可能是上一轮允许有限期证据参与裁决的独立依据。
            eligible.clear()
            continue
        closed: dict[FactId, Fact] = {}
        triggers = _apply_cutoffs(cutoffs, facts, incoming, groups, closed, changed_at)
        covered = {
            source: target
            for source, target in deferred.items()
            if target not in uncertain
            and target in closed
            and _covers_interval(incoming[source], closed[target])
        }
        if covered == eligible:
            return closed, triggers, uncertain
        eligible = covered


def _project_conflicts(
    comparisons: Sequence[FactComparison],
    facts: Mapping[FactId, Fact],
    existing: Mapping[FactId, Fact],
    new_ids: set[FactId],
    projectable_targets: Mapping[FactId, FactId],
    suspended: set[FactId],
) -> tuple[list[_Cutoff], set[FactId]]:
    targets = {fact_id: fact_id for fact_id in existing}
    targets.update(projectable_targets)
    projected: dict[FactPair, list[tuple[Verdict, FactPair]]] = defaultdict(list)
    uncertain: set[FactId] = set()
    for comparison in comparisons:
        source_left, source_right = comparison.pair
        if (
            suspended.intersection(comparison.pair)
            or source_left not in targets
            or source_right not in targets
        ):
            continue
        left, right = targets[source_left], targets[source_right]
        if left == right:
            if comparison.verdict == "conflict" and left in new_ids:
                uncertain.add(left)
            continue
        if comparison.disjoint or intervals_disjoint(facts[left], facts[right]):
            continue
        if left not in new_ids and right not in new_ids:
            continue
        if left.value > right.value:
            left, right = right, left
            source_left, source_right = source_right, source_left
        projected[(left, right)].append(
            (comparison.verdict, (source_left, source_right))
        )

    cutoffs: list[_Cutoff] = []
    existing_successors: dict[FactId, list[_Cutoff]] = defaultdict(list)
    for pair, decisions in projected.items():
        verdicts = {verdict for verdict, _ in decisions}
        left, right = pair
        if len(verdicts) > 1 or "unknown" in verdicts:
            uncertain.update(new_ids.intersection(pair))
        elif "conflict" in verdicts:
            ordered = _order_by_start_time(facts[left], facts[right])
            if ordered is None:
                uncertain.update(new_ids.intersection(pair))
                continue
            earlier, later = ordered
            assert earlier.valid_at is not None and later.valid_at is not None
            trigger_sources = tuple(
                source_left if later.id == left else source_right
                for _, (source_left, source_right) in decisions
            )
            cutoff = _Cutoff(
                earlier.id, later.id, trigger_sources, earlier.valid_at, later.valid_at
            )
            cutoffs.append(cutoff)
            if later.id in existing:
                existing_successors[earlier.id].append(cutoff)

    # 已知的后继都来自同一快照，补录应一次确定最近的有效截止。
    earliest_successors = {
        min(
            successors,
            key=lambda cutoff: (
                cutoff.valid_end,
                existing[cutoff.later_id].recorded_at,
                cutoff.later_id.value,
            ),
        )
        for successors in existing_successors.values()
    }
    return [
        cutoff
        for cutoff in cutoffs
        if cutoff.later_id not in existing or cutoff in earliest_successors
    ], uncertain


def _append_support(
    fact: Fact,
    evidence: Fact,
    existing_support_groups: Mapping[FactId, SupportGroups],
    updated: dict[FactId, Fact],
    support_groups: dict[FactId, SupportGroups],
) -> None:
    target = fact.id
    groups = (
        support_groups[target]
        if target in support_groups
        else existing_support_groups[target]
    )
    sensitive = fact.sensitive or evidence.sensitive
    profile_groups = tuple(
        sorted(set(fact.profile_groups) | set(evidence.profile_groups))
    )
    members = frozenset(evidence.provenance)
    if any(frozenset(existing) == members for existing in groups):
        if sensitive != fact.sensitive or profile_groups != fact.profile_groups:
            updated[target] = replace(
                fact, sensitive=sensitive, profile_groups=profile_groups
            )
        return
    groups = (*groups, evidence.provenance)
    support_groups[target] = groups
    updated[target] = replace(
        fact,
        provenance=flatten_support_groups(groups),
        sensitive=sensitive,
        profile_groups=profile_groups,
    )


def _apply_cutoffs(
    cutoffs: Sequence[_Cutoff],
    facts: Mapping[FactId, Fact],
    incoming: Mapping[FactId, PreparedFact],
    existing_support_groups: Mapping[FactId, SupportGroups],
    updated: dict[FactId, Fact],
    changed_at: float,
) -> dict[FactId, tuple[CutoffTrigger, ...]]:
    triggers: dict[FactId, list[CutoffTrigger]] = defaultdict(list)
    # 先确定触发事实的最终区间。同一事实的多个截止仍按原裁决顺序应用。
    for cutoff in sorted(cutoffs, key=lambda item: item.earlier_valid_at, reverse=True):
        earlier_id, later_id = cutoff.earlier_id, cutoff.later_id
        earlier = updated.get(earlier_id, facts[earlier_id])
        later = updated.get(later_id, facts[later_id])
        if intervals_disjoint(earlier, later):
            continue
        if later_id in incoming:
            source = next(
                (
                    incoming[source_id]
                    for source_id in cutoff.compared_trigger_fact_ids
                    if source_id == later_id
                    or _covers_interval(incoming[source_id], later)
                ),
                None,
            )
            if source is None:
                continue
            group = source.fact.provenance
        else:
            groups = existing_support_groups[later_id]
            group = groups[0] if groups else ()
        valid_end = cutoff.valid_end
        updated[earlier_id] = (
            earlier.close(valid_end, max(changed_at, earlier.recorded_at))
            if earlier.invalid_at is None
            else earlier.shorten_validity(valid_end)
        )
        triggers[earlier_id].append(CutoffTrigger(later_id, valid_end, group))
    return {fact_id: tuple(items) for fact_id, items in triggers.items()}
