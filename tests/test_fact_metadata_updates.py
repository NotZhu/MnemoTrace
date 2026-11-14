"""The remaining product metadata edit leaves the assertion and history intact."""
from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine, seed_timeline


@pytest.mark.parametrize('sensitive', [True, False])
def test_sensitivity_update_preserves_superseded_fact(sensitive):
    memory = MemoryEngine()
    first = memory.add_fact("我", "works_at", "First Lab", valid_at=10, sensitive=False,
                            categories=["professional_details"], text="User works at First Lab.")
    current = seed_timeline(memory, "我", "works_at", "Second Lab", valid_at=20,
                            text="User works at Second Lab.")
    before = asdict(first)
    after = memory.set_fact_sensitive(first.id, sensitive)
    assert asdict(after) == {**before, 'sensitive': sensitive}
    assert not after.is_live() and current.supersedes == [first.id]


def test_sensitivity_update_rejects_non_boolean_without_mutating_fact():
    memory = MemoryEngine()
    fact = memory.add_fact("我", "likes", "Tea", sensitive=False, text="User likes tea.")
    before = asdict(fact)
    with pytest.raises(ValueError, match="boolean"):
        memory.set_fact_sensitive(fact.id, "false")
    assert asdict(memory.fact_store.get(fact.id)) == before
