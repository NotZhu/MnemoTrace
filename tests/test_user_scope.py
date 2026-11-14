from __future__ import annotations


from tests.model_fixtures import MemoryEngine
from mnemotrace.types import Fact


def test_default_user_is_still_supported():
    mem = MemoryEngine()
    fact = mem.add_fact("我", "lives_in", "Paris", text='user lives in Paris')
    assert fact.user_id == "default"
    assert fact.id in {f.id for f in mem.search("Where does user live?").facts}
    assert mem.search("Where does user live?", user_id="other").abstained


def test_profile_uses_live_hot_facts_for_user_and_prefers_authoritative_values():
    mem = MemoryEngine()
    facts = [
        Fact(subject="我", predicate="works_at", object="manual", user_id="user123",
             source="user", valid_at=1, text='user works at manual'),
        Fact(subject="我", predicate="works_at", object="newer extracted", user_id="user123",
             valid_at=2, text='user works at newer extracted'),
        Fact(subject="我", predicate="occupation", object="older", user_id="user123", valid_at=1, text='user occupation older'),
        Fact(subject="我", predicate="occupation", object="newer", user_id="user123", valid_at=2, text='user occupation newer'),
        Fact(subject="我", predicate="lives_in", object="expired", user_id="user123", invalid_at=1, text='user lives in expired'),
        Fact(subject="我", predicate="born_in", object="foreign", user_id="other", text='user born in foreign'),
    ]
    for fact in facts:
        mem.fact_store.upsert(fact.id, [], fact)
    cold = Fact(subject="我", predicate="favorite_food", object="cold", user_id="user123", text='user favorite food cold')
    mem.cold_store.upsert(cold.id, [], cold)

    assert mem.profile("user123")["updated_at"] is None
