"""验证事实断言、溯源和两条时间轴。"""

from dataclasses import replace
from typing import cast

import pytest

from mnemotrace.domain.enums import Source
from mnemotrace.domain.errors import InvalidFact
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import EntityId, FactId, MessageId, SpaceId
from mnemotrace.domain.values import (
    ContentHash,
    EntityRef,
    Label,
    LiteralValue,
    Predicate,
)


@pytest.fixture
def fact() -> Fact:
    """为事实生命周期测试提供起始快照。

    Returns:
        有生效时间及来源的未截止事实。
    """
    return Fact(
        id=FactId("fact"),
        space_id=SpaceId("space"),
        subject=EntityRef(EntityId("alice")),
        predicate=Predicate("likes"),
        object=EntityRef(EntityId("tea")),
        text="　Ａｌｉｃｅ\tlikes\ntea ",
        valid_at=5,
        invalid_at=None,
        recorded_at=10,
        expired_at=None,
        provenance=(MessageId("m2"), MessageId("m1"), MessageId("m2")),
        labels=(Label("sports"), Label("food"), Label("sports")),
        sensitive=False,
        source=Source.EXTRACTED,
    )


def test_fact_normalizes_provenance_and_labels_and_derives_hash(fact: Fact) -> None:
    assert fact.text == "　Ａｌｉｃｅ\tlikes\ntea "
    assert fact.provenance == (MessageId("m2"), MessageId("m1"))
    assert fact.labels == (Label("food"), Label("sports"))
    assert fact.is_edge
    assert fact.content_hash == ContentHash(
        "31006013baec0b33f6cfc3a2ec44bf3c44d41fe664ec118c093436152c4a2e55"
    )
    assert type(fact.valid_at) is float


def test_manual_literal_fact_can_have_unknown_start_and_no_provenance(
    fact: Fact,
) -> None:
    manual = replace(
        fact,
        object=LiteralValue("茶"),
        source=Source.MANUAL,
        provenance=(),
        valid_at=None,
        invalid_at=30,
        expired_at=10,
    )

    assert manual.valid_at is None
    assert manual.invalid_at == 30.0
    assert manual.provenance == ()
    assert not manual.is_edge


@pytest.mark.parametrize(
    "changes",
    (
        {"text": " \t"},
        {"space_id": "space"},
        {"subject": LiteralValue("Alice")},
        {"predicate": "likes"},
        {"object": "tea"},
        {"source": "extracted"},
        {"sensitive": 1},
        {"provenance": ()},
        {"provenance": [MessageId("m1")]},
        {"labels": ("unknown",)},
    ),
)
def test_fact_rejects_invalid_fields(fact: Fact, changes: dict[str, object]) -> None:
    with pytest.raises(InvalidFact):
        replace(fact, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("invalid_at", "expired_at"),
    ((12, None), (None, 11), (5, 11), (4, 11), (12, 9), (float("inf"), 11)),
)
def test_fact_rejects_invalid_time_pairs_and_intervals(
    fact: Fact, invalid_at: float | None, expired_at: float | None
) -> None:
    with pytest.raises(InvalidFact):
        replace(fact, invalid_at=invalid_at, expired_at=expired_at)


def test_close_records_a_future_end_and_can_only_be_called_once(fact: Fact) -> None:
    closed = fact.close(valid_end=30, closed_at=12)

    assert closed == replace(fact, invalid_at=30, expired_at=12)
    assert fact.invalid_at is None
    assert closed.expired_at == 12.0
    assert closed.recorded_at == 10.0
    with pytest.raises(InvalidFact):
        closed.close(valid_end=25, closed_at=13)


@pytest.mark.parametrize(
    ("valid_end", "closed_at"),
    ((5, 12), (4, 12), (30, 9), (30, float("nan")), (None, None)),
)
def test_close_rejects_invalid_times(
    fact: Fact, valid_end: float | None, closed_at: float | None
) -> None:
    with pytest.raises(InvalidFact):
        fact.close(cast(float, valid_end), cast(float, closed_at))


def test_shorten_validity_keeps_the_first_observation_and_identity(fact: Fact) -> None:
    closed = fact.close(30, 12)
    shortened = closed.shorten_validity(25)

    assert closed.invalid_at == 30.0
    assert shortened == replace(closed, invalid_at=25)
    assert shortened.expired_at == 12.0


@pytest.mark.parametrize("end", (5, 30, 31, float("nan")))
def test_shorten_validity_rejects_invalid_ends(fact: Fact, end: float) -> None:
    with pytest.raises(InvalidFact):
        fact.close(30, 12).shorten_validity(end)


def test_shorten_validity_requires_an_existing_end(fact: Fact) -> None:
    with pytest.raises(InvalidFact):
        fact.shorten_validity(25)
