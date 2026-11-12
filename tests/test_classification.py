"""画像按存储的主题分组，并保留事实与独立的敏感标记。"""
from __future__ import annotations
from tests.model_fixtures import saved_outcome


import pytest

from tests.model_fixtures import MemoryEngine


def test_synthetic_facts_default_to_sensitive_without_inferred_category():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "Storage Labs", text="user works at Storage Labs")
    assert (fact.categories, fact.sensitive) == ([], True)


def test_offline_extraction_stores_model_category():
    mem = MemoryEngine()
    episode = mem.add("I work at Storage Labs.", user_id="synthetic-user")
    mem.consolidate()
    facts = [f for f in mem.fact_store.values() if episode.id in f.provenance]
    assert len(facts) == 1
    assert facts[0].predicate == "works_at" and facts[0].categories == ["professional_details"]


@pytest.mark.parametrize("subject", ["user", "Bank of China", "Health Center", "Age Lab"])
def test_subject_does_not_change_category(subject):
    mem = MemoryEngine()
    fact = mem.add_fact(subject, "uses", "Vim", text=f"{subject} uses Vim")
    assert fact.categories == []


def test_outcome_reason_stays_sensitive():
    mem = MemoryEngine()
    fact = saved_outcome(mem, user_id="default", session_id="synthetic-session", kind="lesson",
                         statement="Choose the synthetic deployment option", why="The password was SYNTHETIC_VALUE")
    assert (fact.categories, fact.sensitive) == ([], True)
