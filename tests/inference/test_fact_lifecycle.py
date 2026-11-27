"""验证归并、有效区间及截止归因的业务规则。"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest

from mnemotrace.application.adjudication import (
    FactComparison,
    PreparedFact,
    Verdict,
    prepare_facts,
)
from mnemotrace.application.extraction import ExtractedFact
from mnemotrace.application.fact_lifecycle import FactWrites, calculate_fact_writes
from mnemotrace.domain.enums import ProfileGroup
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import FactId, MessageId
from mnemotrace.domain.values import LiteralValue
from mnemotrace.ports.storage import CutoffTrigger

from ..ports.fakes import FakeEmbedder
from .adjudication_samples import SPACE, FactIds, candidate


def _prepare(*extracted: ExtractedFact) -> tuple[PreparedFact, ...]:
    return asyncio.run(
        prepare_facts(
            extracted,
            space_id=SPACE,
            embedder=FakeEmbedder(),
            ids=FactIds(),
            received_at=100.0,
        )
    )


def _decide(
    prepared: Sequence[PreparedFact],
    old: Sequence[Fact],
    *decisions: FactComparison,
) -> FactWrites:
    return calculate_fact_writes(
        prepared,
        decisions,
        existing_facts={item.id: item for item in old},
        existing_support_groups={
            item.id: (item.provenance,) if item.provenance else () for item in old
        },
        changed_at=110.0,
    )


def test_duplicate_fact_merges_profile_groups_and_keeps_its_identity() -> None:
    old, repeat = _prepare(
        replace(
            candidate("old"), profile_groups=(ProfileGroup.COMMUNICATION_PREFERENCES,)
        ),
        replace(candidate("repeat"), profile_groups=(ProfileGroup.CONSTRAINTS,)),
    )
    result = _decide(
        (repeat,),
        (old.fact,),
        FactComparison((repeat.fact.id, old.fact.id), "duplicate"),
    )
    assert result.facts == (
        replace(
            old.fact,
            provenance=(*old.fact.provenance, *repeat.fact.provenance),
            profile_groups=(
                ProfileGroup.COMMUNICATION_PREFERENCES,
                ProfileGroup.CONSTRAINTS,
            ),
        ),
    )


def test_duplicates_choose_one_stable_target_and_keep_independent_groups() -> None:
    """重复只更新录入时间和 ID 排在最前的旧事实。"""
    old, repeat, same_group = _prepare(
        candidate("old"),
        candidate("repeat", other_evidence=(MessageId("proposal"),)),
        candidate("proposal", other_evidence=(MessageId("repeat"),)),
    )
    selected = replace(old.fact, id=FactId("a"), recorded_at=50.0)
    alternatives = (
        replace(selected, id=FactId("b")),
        replace(selected, id=FactId("0"), recorded_at=60.0),
    )
    conflicting = replace(selected, id=FactId("conflicting"), valid_at=5.0)
    result = _decide(
        (repeat, same_group),
        (*alternatives, selected, conflicting),
        *(
            FactComparison((item.fact.id, target.id), "duplicate")
            for item in (repeat, same_group)
            for target in (*alternatives, selected)
        ),
        FactComparison((repeat.fact.id, conflicting.id), "conflict"),
    )
    assert result.facts == (
        replace(selected, provenance=(*selected.provenance, *repeat.fact.provenance)),
    )
    assert result.support_groups == {
        selected.id: (selected.provenance, repeat.fact.provenance)
    }
    assert not result.cutoff_triggers and not result.vectors
    assert result.entities == ()


def test_later_confirmation_cannot_support_original_start() -> None:
    """较晚的确认只支持其已知时段，无法替代早期来源。"""
    first, late = _prepare(candidate("old", start=10.0), candidate("late", start=20.0))
    result = _decide(
        (late,),
        (first.fact,),
        FactComparison((late.fact.id, first.fact.id), "duplicate"),
    )
    assert result.facts == ()
    assert result.support_groups == {}


@pytest.mark.parametrize(
    ("old_end", "new_end", "adds_support"),
    [
        (30.0, None, False),
        (30.0, 20.0, False),
        (30.0, 30.0, True),
        (30.0, 40.0, True),
        (None, 30.0, False),
    ],
    ids=["missing-end", "earlier-end", "same-end", "later-end", "open-target"],
)
def test_duplicate_evidence_must_cover_original_end(
    old_end: float | None, new_end: float | None, adds_support: bool
) -> None:
    """证据须覆盖旧事实截止，重复裁决保留原有效区间。"""
    old, repeat = _prepare(
        candidate("old", start=10.0, end=old_end),
        candidate("repeat", start=10.0, end=new_end),
    )
    result = _decide(
        (repeat,),
        (old.fact,),
        FactComparison((repeat.fact.id, old.fact.id), "duplicate"),
    )
    assert result.cutoff_triggers == {}
    if adds_support:
        assert result.facts == (
            replace(
                old.fact, provenance=(*old.fact.provenance, *repeat.fact.provenance)
            ),
        )
        assert result.support_groups == {
            old.fact.id: (old.fact.provenance, repeat.fact.provenance)
        }
    else:
        assert result.facts == ()
        assert result.support_groups == {}


def test_duplicate_relation_is_not_transitive() -> None:
    """中间候选已归并时，另一个候选仍须直接重复代表才能归并。"""
    first, second, third = _prepare(
        candidate("one"), candidate("two"), candidate("three")
    )
    result = _decide(
        (first, second, third),
        (),
        FactComparison((first.fact.id, second.fact.id), "duplicate"),
        FactComparison((second.fact.id, third.fact.id), "duplicate"),
    )
    assert [fact.id for fact in result.facts] == [first.fact.id, third.fact.id]
    assert result.support_groups[first.fact.id] == (
        first.fact.provenance,
        second.fact.provenance,
    )
    assert set(result.vectors) == {first.fact.id, third.fact.id}


def test_unknown_allows_direct_support_of_existing_fact_only() -> None:
    """与其他事实不确定时，已确认的旧目标仍可获得完整证据。"""
    original, repeat, other, different = _prepare(
        candidate("old"),
        candidate("repeat"),
        candidate("other"),
        candidate("different", start=5.0),
    )
    result = _decide(
        (repeat, other),
        (original.fact, different.fact),
        FactComparison((repeat.fact.id, original.fact.id), "duplicate"),
        FactComparison((repeat.fact.id, different.fact.id), "conflict"),
        FactComparison((repeat.fact.id, other.fact.id), "unknown"),
    )
    assert [fact.id for fact in result.facts] == [original.fact.id]
    assert result.support_groups[original.fact.id] == (
        original.fact.provenance,
        repeat.fact.provenance,
    )
    assert result.cutoff_triggers == {}


@pytest.mark.parametrize(
    ("start", "other_start", "verdict"),
    [(None, 20.0, "conflict"), (10.0, 10.0, "conflict"), (10.0, 20.0, "unknown")],
    ids=["unknown-start", "same-start", "uncertain-verdict"],
)
def test_uncertain_new_pair_stays_original_only(
    start: float | None, other_start: float, verdict: Verdict
) -> None:
    """无从确认先后或关系时，双方仅保留原文。"""
    first, second = _prepare(
        candidate("one", start=start), candidate("two", start=other_start)
    )
    result = _decide(
        (first, second), (), FactComparison((first.fact.id, second.fact.id), verdict)
    )
    assert result.facts == ()
    assert result.cutoff_triggers == {}


def test_termination_and_multiple_cutoffs_use_actual_application_order() -> None:
    """明确终止可以截止旧事实，每次提前记录实际触发项。"""
    old, later, sooner, same_cutoff = _prepare(
        candidate("old", start=10.0, text="用户参加跑团"),
        candidate("later", start=40.0, text="用户从较晚日期起不再参加跑团"),
        candidate("sooner", start=20.0, text="用户从较早日期起不再参加跑团"),
        candidate("equal", start=20.0, text="用户同日起退出跑团"),
    )
    result = _decide(
        (later, sooner, same_cutoff),
        (old.fact,),
        *(
            FactComparison((item.fact.id, old.fact.id), "conflict")
            for item in (later, sooner, same_cutoff)
        ),
    )
    saved = {fact.id: fact for fact in result.facts}
    assert saved[old.fact.id].invalid_at == 20.0
    assert saved[old.fact.id].expired_at == 110.0
    assert result.cutoff_triggers[old.fact.id] == (
        CutoffTrigger(later.fact.id, 40.0, later.fact.provenance),
        CutoffTrigger(sooner.fact.id, 20.0, sooner.fact.provenance),
    )


def test_bounded_conflict_closes_old_fact_at_later_start() -> None:
    """已有事实被后生效的有限冲突截止，旧区间不再恢复。"""
    old, later = _prepare(
        candidate("old", start=10.0, end=50.0, text="从 10 至 50 秒默认用英文"),
        candidate(
            "later",
            start=20.0,
            end=30.0,
            text="从 20 至 30 秒默认用中文",
            target=LiteralValue("中文"),
        ),
    )
    result = _decide(
        (later,), (old.fact,), FactComparison((later.fact.id, old.fact.id), "conflict")
    )
    assert {fact.id: (fact.valid_at, fact.invalid_at) for fact in result.facts} == {
        old.fact.id: (10.0, 20.0),
        later.fact.id: (20.0, 30.0),
    }
    assert result.cutoff_triggers[old.fact.id] == (
        CutoffTrigger(later.fact.id, 20.0, later.fact.provenance),
    )


@pytest.mark.parametrize(
    "nearest_first", [True, False], ids=["nearest-first", "nearest-last"]
)
def test_earliest_existing_successor_closes_backfill(nearest_first: bool) -> None:
    """补录只记录最早生效的已有冲突事实及其证据组。"""
    nearest, latest, historical = _prepare(
        candidate("nearest", start=60.0, end=80.0, text="60 至 80 秒期间默认用英文"),
        candidate("latest", start=80.0, text="从 80 秒起默认用日文"),
        candidate("historical", start=20.0, text="从 20 秒起默认用中文"),
    )
    successors = (nearest, latest) if nearest_first else (latest, nearest)
    result = _decide(
        (historical,),
        (nearest.fact, latest.fact),
        *(
            FactComparison((historical.fact.id, successor.fact.id), "conflict")
            for successor in successors
        ),
    )
    (saved,) = result.facts
    assert saved.invalid_at == 60.0
    assert result.cutoff_triggers[saved.id] == (
        CutoffTrigger(nearest.fact.id, 60.0, nearest.fact.provenance),
    )
    assert result.vectors == {saved.id: historical.vector}


def test_cutoff_enables_duplicate_evidence_to_cover_final_interval() -> None:
    """有独立矛盾触发截止时，原本只能覆盖部分区间的证据可补入。"""
    original, matching, partial, termination = _prepare(
        candidate("old", start=10.0, text="用户从 10 秒起参加跑团"),
        candidate(
            "matching", start=10.0, end=30.0, text="用户在 10 至 30 秒期间参加跑团"
        ),
        candidate(
            "partial", start=10.0, end=20.0, text="用户在 10 至 20 秒期间参加跑团"
        ),
        candidate("termination", start=30.0, text="用户从 30 秒起不再参加跑团"),
    )
    result = _decide(
        (matching, partial, termination),
        (original.fact,),
        FactComparison((matching.fact.id, original.fact.id), "duplicate"),
        FactComparison((partial.fact.id, original.fact.id), "duplicate"),
        FactComparison((termination.fact.id, original.fact.id), "conflict"),
    )
    updated = next(fact for fact in result.facts if fact.id == original.fact.id)
    assert updated.invalid_at == 30.0
    assert updated.provenance == (*original.fact.provenance, *matching.fact.provenance)
    assert result.support_groups[original.fact.id] == (
        original.fact.provenance,
        matching.fact.provenance,
    )
    assert result.cutoff_triggers[original.fact.id] == (
        CutoffTrigger(termination.fact.id, 30.0, termination.fact.provenance),
    )


def test_duplicate_evidence_is_checked_against_the_final_cutoff() -> None:
    """缺少截止依据的重复证据在同批或分批写入时都不能支撑已截止事实。"""
    original, repeat, termination = _prepare(
        candidate("old", start=10.0, text="用户从 10 秒起参加跑团"),
        candidate("repeat", start=10.0, text="用户确认从 10 秒起参加跑团"),
        candidate("termination", start=30.0, text="用户从 30 秒起不再参加跑团"),
    )
    duplicate = FactComparison((repeat.fact.id, original.fact.id), "duplicate")
    conflict = FactComparison((termination.fact.id, original.fact.id), "conflict")
    combined = _decide((repeat, termination), (original.fact,), duplicate, conflict)
    terminated = _decide((termination,), (original.fact,), conflict)
    closed = next(fact for fact in terminated.facts if fact.id == original.fact.id)
    later = _decide((repeat,), (closed,), duplicate)

    assert closed.invalid_at == 30.0
    assert later.facts == ()
    assert combined == terminated


def test_conflicting_judgments_about_one_representative_do_not_write_it() -> None:
    """归并后指向同一事实对的矛盾判定使新增代表暂缓。"""
    old, representative, same = _prepare(
        candidate("old", start=10.0),
        candidate("new", start=30.0),
        candidate("same", start=30.0),
    )
    result = _decide(
        (representative, same),
        (old.fact,),
        FactComparison((representative.fact.id, old.fact.id), "coexists"),
        FactComparison((representative.fact.id, same.fact.id), "duplicate"),
        FactComparison((same.fact.id, old.fact.id), "conflict"),
    )
    assert result.facts == ()


def test_batch_change_keeps_disjoint_facts_and_uses_later_evidence() -> None:
    """同批两条状态按时间截止，错开的同值事实仍分别保存。"""
    historical, earlier, later = _prepare(
        candidate("old", start=5.0, end=10.0),
        candidate("earlier", start=10.0, other_evidence=(MessageId("later"),)),
        candidate("later", start=20.0),
    )
    result = _decide(
        (historical, earlier, later),
        (),
        FactComparison(
            (historical.fact.id, earlier.fact.id), "coexists", disjoint=True
        ),
        FactComparison((earlier.fact.id, later.fact.id), "conflict"),
    )
    assert [fact.invalid_at for fact in result.facts] == [10.0, 20.0, None]
    assert result.cutoff_triggers[earlier.fact.id] == (
        CutoffTrigger(later.fact.id, 20.0, later.fact.provenance),
    )


def test_projected_cutoff_uses_the_compared_support_group() -> None:
    """归并到较早代表后，截止归因仍引用实际参与比较的独立证据组。"""
    old, representative, corroboration = _prepare(
        candidate("old", start=10.0),
        candidate("representative", start=20.0),
        candidate("confirmation", start=20.0, other_evidence=(MessageId("proposal"),)),
    )
    result = _decide(
        (representative, corroboration),
        (old.fact,),
        FactComparison((representative.fact.id, corroboration.fact.id), "duplicate"),
        FactComparison((corroboration.fact.id, old.fact.id), "conflict"),
    )
    assert result.support_groups[representative.fact.id] == (
        representative.fact.provenance,
        corroboration.fact.provenance,
    )
    assert result.cutoff_triggers == {
        old.fact.id: (
            CutoffTrigger(representative.fact.id, 20.0, corroboration.fact.provenance),
        )
    }


@pytest.mark.parametrize("direct_conflict", [False, True], ids=["alias-only", "direct"])
def test_cutoff_requires_support_retained_by_new_representative(
    direct_conflict: bool,
) -> None:
    """重复证据未通过最终区间验收时，截止只能采用仍有效的比较依据。"""
    old, representative, alias, later = _prepare(
        candidate("old", start=10.0, text="用户默认用法文"),
        candidate("representative", start=20.0, text="用户默认用英文"),
        candidate("alias", start=20.0, text="用户确认默认用英文"),
        candidate("later", start=40.0, text="用户默认用中文"),
    )
    comparisons = [
        FactComparison((representative.fact.id, alias.fact.id), "duplicate"),
        FactComparison((alias.fact.id, old.fact.id), "conflict"),
        FactComparison((later.fact.id, representative.fact.id), "conflict"),
    ]
    if direct_conflict:
        comparisons.append(
            FactComparison((representative.fact.id, old.fact.id), "conflict")
        )

    result = _decide((representative, alias, later), (old.fact,), *comparisons)
    saved = {fact.id: fact for fact in result.facts}
    assert saved[representative.fact.id].invalid_at == 40.0
    assert result.support_groups[representative.fact.id] == (
        representative.fact.provenance,
    )
    if direct_conflict:
        assert saved[old.fact.id].invalid_at == 20.0
        assert result.cutoff_triggers[old.fact.id] == (
            CutoffTrigger(representative.fact.id, 20.0, representative.fact.provenance),
        )
    else:
        assert old.fact.id not in saved
        assert old.fact.id not in result.cutoff_triggers


@pytest.mark.parametrize(
    "closes_representative", [True, False], ids=["grounded", "open-representative"]
)
def test_newly_covering_support_can_trigger_a_cutoff(
    closes_representative: bool,
) -> None:
    """有限期证据在代表被独立截止后参与冲突，并留下实际引用。"""
    old, representative, alias, later = _prepare(
        candidate("old", start=10.0, text="用户默认用法文"),
        candidate("representative", start=20.0, text="用户默认用英文"),
        candidate("alias", start=20.0, end=40.0, text="该时段默认用英文"),
        candidate("later", start=40.0, text="用户默认用中文"),
    )
    comparisons = [
        FactComparison((representative.fact.id, alias.fact.id), "duplicate"),
        FactComparison((alias.fact.id, old.fact.id), "conflict"),
    ]
    if closes_representative:
        comparisons.extend(
            (
                FactComparison((later.fact.id, old.fact.id), "conflict"),
                FactComparison((later.fact.id, representative.fact.id), "conflict"),
            )
        )
    items = (
        (representative, alias, later)
        if closes_representative
        else (representative, alias)
    )
    result = _decide(items, (old.fact,), *comparisons)
    saved = {fact.id: fact for fact in result.facts}
    if closes_representative:
        assert saved[representative.fact.id].invalid_at == 40.0
        assert saved[old.fact.id].invalid_at == 20.0
        assert result.support_groups[representative.fact.id] == (
            representative.fact.provenance,
            alias.fact.provenance,
        )
        assert result.cutoff_triggers[old.fact.id] == (
            CutoffTrigger(representative.fact.id, 20.0, alias.fact.provenance),
        )
    else:
        assert saved[representative.fact.id].invalid_at is None
        assert old.fact.id not in saved
        assert result.support_groups[representative.fact.id] == (
            representative.fact.provenance,
        )
        assert result.cutoff_triggers == {}


def test_newly_covering_support_can_enable_an_earlier_cutoff() -> None:
    """后继先确认较新的证据组，再沿时间线确认较早的证据组。"""
    old, first, first_alias, second, second_alias, latest = _prepare(
        candidate("old", start=10.0, text="用户默认用法文"),
        candidate("first", start=20.0, text="用户默认用英文"),
        candidate("first-alias", start=20.0, end=30.0, text="该时段默认用英文"),
        candidate("second", start=30.0, text="用户默认用中文"),
        candidate("second-alias", start=30.0, end=40.0, text="该时段默认用中文"),
        candidate("latest", start=40.0, text="用户默认用日文"),
    )
    result = _decide(
        (first, first_alias, second, second_alias, latest),
        (old.fact,),
        FactComparison((first.fact.id, first_alias.fact.id), "duplicate"),
        FactComparison((second.fact.id, second_alias.fact.id), "duplicate"),
        FactComparison((latest.fact.id, second.fact.id), "conflict"),
        FactComparison((second_alias.fact.id, first.fact.id), "conflict"),
        FactComparison((first_alias.fact.id, old.fact.id), "conflict"),
    )
    assert {fact.id: fact.invalid_at for fact in result.facts} == {
        old.fact.id: 20.0,
        first.fact.id: 30.0,
        second.fact.id: 40.0,
        latest.fact.id: None,
    }
    assert result.cutoff_triggers[old.fact.id] == (
        CutoffTrigger(first.fact.id, 20.0, first_alias.fact.provenance),
    )
    assert result.cutoff_triggers[first.fact.id] == (
        CutoffTrigger(second.fact.id, 30.0, second_alias.fact.provenance),
    )


@pytest.mark.parametrize("disputed", [True, False], ids=["withdrawn", "retained"])
def test_duplicate_evidence_requires_a_surviving_independent_cutoff(
    disputed: bool,
) -> None:
    """独立截止依据暂缓后，已入围的有限期证据不能维持代表事实的截止。"""
    first, first_alias, other, other_alias, independent, earlier = _prepare(
        candidate("first", start=20.0),
        candidate("first-alias", start=20.0, end=45.0),
        candidate("other", start=15.0),
        candidate("other-alias", start=15.0, end=45.0),
        candidate("independent", start=40.0),
        candidate("earlier", start=35.0),
    )
    result = _decide(
        (first, first_alias, other, other_alias, independent, earlier),
        (),
        FactComparison((first.fact.id, first_alias.fact.id), "duplicate"),
        FactComparison((other.fact.id, other_alias.fact.id), "duplicate"),
        FactComparison((first.fact.id, independent.fact.id), "conflict"),
        FactComparison((other.fact.id, independent.fact.id), "conflict"),
        FactComparison((first_alias.fact.id, earlier.fact.id), "conflict"),
        FactComparison(
            (other_alias.fact.id, independent.fact.id),
            "coexists" if disputed else "conflict",
        ),
    )
    saved = {fact.id: fact for fact in result.facts}
    if disputed:
        assert set(saved) == {first.fact.id, earlier.fact.id}
        assert saved[first.fact.id] == first.fact
        assert result.support_groups[first.fact.id] == (first.fact.provenance,)
        assert not result.cutoff_triggers
    else:
        assert saved[first.fact.id].invalid_at == 35.0
        assert result.support_groups[first.fact.id] == (
            first.fact.provenance,
            first_alias.fact.provenance,
        )
        assert result.cutoff_triggers[first.fact.id][-1] == CutoffTrigger(
            earlier.fact.id, 35.0, earlier.fact.provenance
        )
