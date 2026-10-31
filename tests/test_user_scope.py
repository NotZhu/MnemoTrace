from __future__ import annotations

import json

import pytest

from tests.model_fixtures import Memory, seed_summaries
from tests.model_fixtures import MemoryService
from mnemotrace.types import Fact


def test_user_scopes_preserve_all_memory_layers_after_restart(tmp_path):
    mem = Memory()
    users = {"user123": ("Aster", "Paris"), "User123": ("Beryl", "Berlin")}
    for user, (name, city) in users.items():
        mem.add(f"My name is {name}. I live in {city}.", user_id=user, session_id="shared")
    mem.consolidate()
    for user, (_, city) in users.items():
        mem.remember_working(f"prepare trip to {city}", user_id=user, session_id="shared")
    seed_summaries(mem, mem.episodes_doc.values())
    before = {f.id: f.__dict__.copy() for f in mem._all_facts()}
    mem.save(str(tmp_path / "store"))
    loaded = Memory.open(str(tmp_path / "store"))
    assert {f.id: f.__dict__ for f in loaded._all_facts()} == before
    manifest = json.loads((tmp_path / "store/manifest.json").read_text(encoding="utf-8"))
    assert "resolver_parent" not in manifest["state"]
    assert loaded._identity == mem._identity
    assert loaded._aliases == mem._aliases
    for user, (name, city) in users.items():
        assert loaded.engine.self_name(user) == name
        assert loaded.profile(user)["lives_in"] == city
        assert all(f.user_id == user for f in loaded.search("Where do I live?", user_id=user).facts)
        assert {f.object for f in loaded.history(name, "lives_in", user_id=user)} == {city}
        assert {w.content for w in loaded.working_memory(user)} == {f"prepare trip to {city}"}
        assert {e.user_id for e in loaded.retrieve_episodes("I live", user_id=user)} == {user}
        assert {e.user_id for e in loaded.retrieve_summaries("I live", user_id=user)} == {user}
        names = {e["name"] for e in loaded.graph_data(user)["nodes"]}
        assert name in names and city in names
        assert not names.intersection({other_city for _, other_city in users.values()} - {city})
    assert loaded.search("Where do I live?", user_id="unrelated").abstained
    assert loaded.working_memory("unrelated") == []
    assert loaded.retrieve_episodes("I live", user_id="unrelated") == []


def test_default_user_is_still_supported():
    mem = Memory()
    fact = mem.add_fact("user", "lives_in", "Paris")
    assert fact.user_id == "default"
    assert fact.id in {f.id for f in mem.search("Where does user live?").facts}
    assert mem.search("Where does user live?", user_id="other").abstained


def test_profile_uses_live_hot_facts_for_user_and_prefers_authoritative_values():
    mem = Memory()
    facts = [
        Fact(subject="user123", predicate="works_at", object="manual", user_id="user123",
             source="user", valid_at=1),
        Fact(subject="user123", predicate="works_at", object="newer extracted", user_id="user123",
             valid_at=2),
        Fact(subject="user123", predicate="occupation", object="older", user_id="user123", valid_at=1),
        Fact(subject="user123", predicate="occupation", object="newer", user_id="user123", valid_at=2),
        Fact(subject="user123", predicate="lives_in", object="expired", user_id="user123", invalid_at=1),
        Fact(subject="user123", predicate="born_in", object="foreign", user_id="other"),
    ]
    for fact in facts:
        mem.fact_store.upsert(fact.id, [], fact)
    cold = Fact(subject="user123", predicate="favorite_food", object="cold", user_id="user123")
    mem.cold_store.upsert(cold.id, [], cold)

    assert mem.profile("user123") == {"works_at": "manual", "occupation": "newer"}


@pytest.mark.parametrize("mapping", [{}, {"user123": "user123"}, None, [], "invalid", {"user123": 7}, {"user123": "other"}])
def test_unknown_state_fields_do_not_change_record_identity(tmp_path, mapping):
    source = tmp_path / "store"
    mem = Memory()
    fact = mem.add_fact("user", "lives_in", "Paris", user_id="user123")
    mem.save(str(source))
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["state"]["resolver_parent"] = mapping
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = Memory.open(str(source))
    assert loaded.fact_store.get(fact.id).__dict__ == fact.__dict__
    loaded.save()
    assert "resolver_parent" not in json.loads(manifest_path.read_text(encoding="utf-8"))["state"]


def test_audit_and_clear_slot_share_exact_scope_and_protect_history(tmp_path):
    svc = MemoryService(data_dir=str(tmp_path))
    try:
        mem = svc.get("user123")
        kept = mem.add_fact("user", "occupation", "owner", user_id="user123")
        old = Fact(subject="user", predicate="occupation", object="old", user_id="user123", invalid_at=1)
        foreign = Fact(subject="user", predicate="occupation", object="foreign", user_id="User123")
        for f in (old, foreign):
            f.embedding = mem.embedder.embed(f.text)
            mem.fact_store.upsert(f.id, f.embedding, f)
        junk = []
        for i in range(3):
            f = Fact(subject="user", predicate="occupation", object=f"borrowed title {i}", user_id="user123")
            f.embedding = mem.embedder.embed(f.text)
            mem.fact_store.upsert(f.id, f.embedding, f)
            junk.append(f)
        finding = next(f for f in svc.audit("user123")["findings"] if f["kind"] == "slot_overflow")
        assert finding["count"] == 3
        assert {f["fact_id"] for f in finding["samples"]} == {f.id for f in junk}
        assert not svc.clear_slot("user123", "user", "occupation", expect_count=2)["ok"]
        assert all(mem.fact_store.get(f.id) is not None for f in junk)
        assert svc.clear_slot("user123", "user", "occupation", expect_count=3)["deleted"] == 3
        assert all(mem.fact_store.get(f.id) is None for f in junk)
        assert all(mem.fact_store.get(f.id) is not None for f in (kept, old, foreign))
    finally:
        svc.close()
