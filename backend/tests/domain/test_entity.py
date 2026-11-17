"""验证实体的确定性身份和类型变更。"""

from dataclasses import replace

import pytest

from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.errors import InvalidEntity
from mnemotrace_next.domain.ids import EntityId, MessageId, SpaceId


def test_entity_derives_a_fixed_id_from_its_space_and_normalized_name() -> None:
    original_name = "　ＡlIce\t  Smith "
    entity = Entity(SpaceId("space-a"), original_name, "  ")

    assert entity.name == original_name
    assert entity.name_key == "alice smith"
    assert entity.id == EntityId("9d1e461f-625d-5465-bb8c-74d3f47743c4")
    assert entity.type is None
    assert Entity(SpaceId("space-a"), "Alice Smith").id == entity.id
    assert Entity(SpaceId("space-b"), "Alice Smith").id != entity.id


def test_entity_id_keeps_space_and_name_separate_with_pipe_characters() -> None:
    first = Entity(SpaceId("a|b"), "c")
    second = Entity(SpaceId("a"), "b|c")

    assert first.id != second.id


def test_with_type_returns_a_new_entity_with_the_same_id() -> None:
    original = Entity(SpaceId("space"), "Alice", "person")
    changed = original.with_type(" colleague ")

    assert changed.id == original.id
    assert changed.name_key == original.name_key
    assert changed.type == " colleague "
    assert original.type == "person"
    assert changed.with_type("").type is None


@pytest.mark.parametrize(
    "changes",
    (
        {"name": "　\t"},
        {"name": "\ud800"},
        {"space_id": MessageId("space")},
        {"type": 1},
    ),
)
def test_entity_rejects_invalid_fields(changes: dict[str, object]) -> None:
    entity = Entity(SpaceId("space"), "Alice")
    with pytest.raises(InvalidEntity):
        replace(entity, **changes)  # type: ignore[arg-type]
