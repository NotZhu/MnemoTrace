"""验证名称规范化、图引用和受控值对象。"""

from typing import cast

import pytest

from mnemotrace.domain.errors import DomainError
from mnemotrace.domain.ids import EntityId, SpaceId
from mnemotrace.domain.values import (
    EntityRef,
    Label,
    LiteralValue,
    Predicate,
    normalize_name,
)


def test_normalization_folds_names() -> None:
    original = "　ＡＬＩＣＥ \t Straße\n "

    assert normalize_name(original) == "alice strasse"
    assert Predicate(original).value == "alice strasse"


def test_entity_ref_requires_an_entity_id() -> None:
    entity_id = EntityId("entity")
    assert EntityRef(entity_id).entity_id == entity_id

    with pytest.raises(DomainError):
        EntityRef(cast(EntityId, SpaceId("entity")))


def test_literal_value_preserves_text_and_normalizes_blank_kind() -> None:
    assert LiteralValue(" 茶 ", " \t").value == " 茶 "
    assert LiteralValue("茶", "").kind is None
    assert LiteralValue("茶", " beverage ").kind == " beverage "


def test_blank_literal_and_normalized_predicate_are_rejected() -> None:
    with pytest.raises(DomainError):
        LiteralValue(" \t ")
    with pytest.raises(DomainError):
        Predicate("　\n ")


def test_label_requires_an_exact_known_value() -> None:
    assert Label("personal_details").value == "personal_details"

    with pytest.raises(DomainError):
        Label("Sports")
    with pytest.raises(DomainError):
        Label("unknown")
