"""画像按存储的主题分组，并保留事实与独立的敏感标记。"""
from __future__ import annotations
from tests.model_fixtures import saved_outcome


import pytest

from tests.model_fixtures import MemoryEngine


def test_manual_writes_default_to_sensitive_without_inferred_category():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "Storage Labs")
    assert (fact.categories, fact.sensitive) == ([], True)
    mem.update_fact(fact.id, predicate="uses", object="storage engine")
    assert fact.categories == []
    mem.update_fact(fact.id, predicate="prefers", object="team collaboration")
    assert fact.categories == []

    manual = mem.add_fact("user", "uses", "storage engine", categories=["technology"], sensitive=True)
    assert (manual.categories, manual.sensitive) == (["technology"], True)
    mem.update_fact(manual.id, categories=["misc"], sensitive=False)
    assert (manual.categories, manual.sensitive) == (["misc"], False)


def test_offline_extraction_stores_model_category():
    mem = MemoryEngine()
    episode = mem.add("I work at Storage Labs.", user_id="synthetic-user")
    mem.consolidate()
    facts = [f for f in mem.fact_store.values() if episode.id in f.provenance]
    assert len(facts) == 1
    assert facts[0].predicate == "works_at" and facts[0].categories == ["professional_details"]


@pytest.mark.parametrize("subject", ["user", "Bank of China", "Health Center", "Age Lab"])
def test_subject_does_not_change_category_on_add_or_edit(subject):
    mem = MemoryEngine()
    fact = mem.add_fact(subject, "uses", "Vim")
    assert fact.categories == []
    mem.update_fact(fact.id, object="Neovim")
    assert fact.categories == []


def test_existing_category_and_metadata_edits_do_not_reclassify():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "Storage Labs", categories=["personal_details"])
    mem.update_fact(fact.id, sensitive=False)
    assert fact.categories == ["personal_details"]
    mem.update_fact(fact.id, object="Other Storage Labs")
    assert (fact.categories, fact.sensitive) == ([], True)


def test_outcome_reason_stays_sensitive_after_content_edit():
    mem = MemoryEngine()
    fact = saved_outcome(mem, user_id="default", session_id="synthetic-session", kind="lesson",
                         statement="Choose the synthetic deployment option", why="The password was SYNTHETIC_VALUE")
    assert (fact.categories, fact.sensitive) == ([], True)
    mem.update_fact(fact.id, statement="Choose the revised synthetic deployment option")
    assert "SYNTHETIC_VALUE" in fact.text
    assert (fact.categories, fact.sensitive) == ([], True)
