"""验证领域标识的类型、原值和非空约束。"""

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from mnemotrace.domain.errors import DomainError
from mnemotrace.domain.ids import (
    EntityId,
    FactId,
    MessageId,
    SessionId,
    SpaceId,
)

type Identifier = SpaceId | SessionId | MessageId | FactId | EntityId
ID_TYPES = (SpaceId, SessionId, MessageId, FactId, EntityId)


def test_ids_preserve_value_and_compare_by_type() -> None:
    identifiers = [id_type(" stable|id ") for id_type in ID_TYPES]

    assert all(identifier.value == " stable|id " for identifier in identifiers)
    assert len(set(identifiers)) == len(ID_TYPES)
    assert SpaceId("space") == SpaceId("space")


def test_id_is_frozen() -> None:
    identifier = MessageId("message")
    with pytest.raises(FrozenInstanceError):
        identifier.value = "other"  # type: ignore[misc]


@pytest.mark.parametrize("id_type", ID_TYPES)
@pytest.mark.parametrize("value", ("", " \t\n", 7))
def test_ids_reject_blank_or_non_string_values(
    id_type: type[Identifier], value: object
) -> None:
    with pytest.raises(DomainError):
        id_type(cast(str, value))
