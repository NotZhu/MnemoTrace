"""验证空间字段与纪元秒的共同约束。"""

from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from mnemotrace.domain.errors import InvalidSpace
from mnemotrace.domain.ids import MessageId, SpaceId
from mnemotrace.domain.space import Space


def test_space_preserves_display_name_and_is_frozen() -> None:
    space = Space(SpaceId("space"), "owner", " 研究 ", -2)

    assert space.name == " 研究 "
    assert space.created_at == -2.0
    assert type(space.created_at) is float
    with pytest.raises(FrozenInstanceError):
        space.name = "其他"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    ({"name": " \n"}, {"owner_id": "\t"}, {"id": MessageId("space")}),
)
def test_space_rejects_invalid_identity_or_text(changes: dict[str, object]) -> None:
    space = Space(SpaceId("space"), "owner", "研究", 0)
    with pytest.raises(InvalidSpace):
        replace(space, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "at",
    (True, float("nan"), float("inf"), float("-inf"), 10**400, "1", None),
    ids=(
        "bool",
        "nan",
        "positive-inf",
        "negative-inf",
        "overflow",
        "string",
        "missing",
    ),
)
def test_space_rejects_non_finite_or_non_numeric_time(at: object) -> None:
    with pytest.raises(InvalidSpace):
        Space(SpaceId("space"), "owner", "研究", cast(float, at))
