"""验证原文规范化、图引用和受控值对象。"""

from typing import cast

import pytest

from mnemotrace_next.domain.errors import DomainError
from mnemotrace_next.domain.ids import EntityId, SpaceId
from mnemotrace_next.domain.values import (
    ContentHash,
    EntityRef,
    Label,
    LiteralValue,
    Predicate,
    content_hash,
    normalize_name,
    normalize_text,
)


def test_normalization_preserves_text_case_and_folds_names() -> None:
    original = "　ＡＬＩＣＥ \t Straße\n "

    assert normalize_text(original) == "ALICE Straße"
    assert normalize_name(original) == "alice strasse"
    assert Predicate(original).value == "alice strasse"


def test_content_hash_uses_normalized_utf8_text() -> None:
    digest = content_hash("　Ａｌｉｃｅ\tlikes\ntea ")

    assert digest == "31006013baec0b33f6cfc3a2ec44bf3c44d41fe664ec118c093436152c4a2e55"
    assert ContentHash(digest).value == digest
    assert content_hash("alice likes tea") != digest


@pytest.mark.parametrize("value", ("a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n"))
def test_content_hash_rejects_the_wrong_format(value: str) -> None:
    with pytest.raises(DomainError):
        ContentHash(value)


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
