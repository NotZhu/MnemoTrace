"""Structured migration validates the whole batch before touching target data."""
from __future__ import annotations

import copy
from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine
from mnemotrace.types import Episode, Fact


def snapshot():
    return {
        "format_version": 6,
        "facts": [asdict(Fact(id="ft_source", subject="Synthetic", predicate="works_at",
                              object="SyntheticCo", valid_at=0, created_at=-1, source="user", text='Synthetic works at SyntheticCo'))],
        "episodes": [asdict(Episode(id="ep_source", content="Synthetic source", event_time=0))],
    }


def target():
    mem = MemoryEngine()
    mem.add_fact("Existing", "likes", "tea", user_id="target", text='Existing likes tea')
    mem.set_focus(track=["existing"], mute=["muted"])
    mem.set_policy(extract_instruction="existing policy")
    mem._aliases = {"target": {"Existing"}}
    return mem


def contents(mem):
    return copy.deepcopy({
        "format_version": 6,
        "facts": [asdict(f) for f in mem._all_facts()],
        "episodes": [asdict(ep) for ep in mem.episodes_doc.values()],
        "entities": [asdict(e) for e in mem.graph.entities.values()],
        "relations": [asdict(r) for r in mem.graph.relations()],
        "focus": mem.focus, "policy": mem.policy,
        "aliases": mem._aliases,
    })


@pytest.mark.parametrize("key", ["facts", "episodes"])
def test_missing_envelope_rejects_entire_batch(key):
    payload = snapshot()
    payload.pop(key)
    mem = target()
    before = contents(mem)
    with pytest.raises(ValueError, match=key):
        mem.import_snapshot(payload)
    assert contents(mem) == before


@pytest.mark.parametrize("key", [
    "id", "subject", "predicate", "object", "text", "source", "valid_at", "invalid_at",
    "created_at", "expired_at", "supersedes", "provenance", "categories", "sensitive",
    "salience", "relation_id", "subject_id", "object_id",
])
def test_missing_fact_history_rejects_before_any_episode_is_added(key):
    payload = snapshot()
    payload["facts"][0].pop(key)
    mem = target()
    before = contents(mem)
    with pytest.raises(ValueError, match=key):
        mem.import_snapshot(payload)
    assert contents(mem) == before


@pytest.mark.parametrize('change', ['old_version', 'display', 'blank_text', 'conflicts', 'withdrawn_at'])
def test_retired_fact_format_is_rejected_without_mutating_target(change):
    payload = snapshot()
    if change == 'old_version':
        payload['format_version'] = 5
    elif change == 'display':
        payload['facts'][0]['display'] = payload['facts'][0]['text']
    elif change == 'blank_text':
        payload['facts'][0]['text'] = ' \n\t'
    elif change == 'conflicts':
        payload['conflicts'] = []
    else:
        payload['facts'][0]['withdrawn_at'] = None
    memory = target()
    before = contents(memory)
    with pytest.raises(ValueError):
        memory.import_snapshot(payload)
    assert contents(memory) == before


@pytest.mark.parametrize("key", ["id", "session_id", "role", "content", "event_time"])
def test_missing_episode_fields_rejects_batch(key):
    payload = snapshot()
    payload["episodes"][0].pop(key)
    with pytest.raises(ValueError, match=key):
        MemoryEngine().import_snapshot(payload)


@pytest.mark.parametrize("key,value", [
    ("valid_at", "2024-01-01"), ("created_at", True), ("invalid_at", float("nan")),
    ("expired_at", float("inf")), ("provenance", "abc"), ("sensitive", "false"),
    ("confidence", "0.8"), ("subject", 42), ("source", "synthetic-secret"),
])
def test_invalid_last_record_does_not_partially_import_or_echo_values(key, value):
    payload = snapshot()
    bad = copy.deepcopy(payload["facts"][0])
    bad["id"] = "ft_bad"
    bad[key] = value
    payload["facts"].append(bad)
    mem = target()
    before = contents(mem)
    with pytest.raises(ValueError, match=r"facts\[1\]") as exc:
        mem.import_snapshot(payload)
    assert "synthetic-secret" not in str(exc.value)
    assert contents(mem) == before


@pytest.mark.parametrize("changes, field", [
    ({"invalid_at": 1}, "invalid_at"),
    ({"invalid_at": 2}, "invalid_at"),
    ({"supersedes": ["ft_bad"]}, "supersedes"),
    ({"supersedes": ["ft_source", "ft_source"]}, "supersedes"),
])
def test_invalid_fact_timeline_rejects_whole_snapshot(changes, field):
    payload = snapshot()
    payload["facts"].append(dict(payload["facts"][0], id="ft_bad", valid_at=2) | changes)
    mem = target()
    before = contents(mem)
    with pytest.raises(ValueError, match=rf"snapshot\.facts\[1\]\.{field}:"):
        mem.import_snapshot(payload)
    assert contents(mem) == before


def test_replacement_cycle_rejects_whole_snapshot():
    payload = snapshot()
    first = payload["facts"][0]
    first["supersedes"] = ["ft_third"]
    payload["facts"].extend([
        dict(first, id="ft_second", supersedes=["ft_source"]),
        dict(first, id="ft_third", supersedes=["ft_second"]),
    ])
    mem = target()
    before = contents(mem)
    with pytest.raises(ValueError, match=r"snapshot\.facts\[\d+\]\.supersedes:.*cycle"):
        mem.import_snapshot(payload)
    assert contents(mem) == before


def test_snapshot_allows_unknown_start_and_omitted_predecessor():
    payload = snapshot()
    payload["facts"][0].update(valid_at=None, invalid_at=2, supersedes=["ft_outside"])
    mem = MemoryEngine()
    assert mem.import_snapshot(payload)["facts_restored"] == 1
    restored, = mem.history("Synthetic", "works_at")
    assert restored.valid_at is None and restored.invalid_at == 2
    assert restored.supersedes == []


def test_unknown_snapshot_fields_are_rejected():
    payload = snapshot()
    payload["mnemotrace_export_version"] = 1
    with pytest.raises(ValueError, match="snapshot: unsupported field"):
        MemoryEngine().import_snapshot(payload)


def test_migration_preserves_source_history_and_target_settings():
    mem = target()
    before = contents(mem)
    payload = snapshot()
    payload["focus"] = {"track": ["source setting"], "mute": []}
    payload["facts"][0]["provenance"] = ["ep_source"]
    payload["episodes"][0].pop("digest")
    stats = mem.import_snapshot(payload, user_id="target")
    assert stats["facts_restored"] == stats["episodes_restored"] == 1
    fact = next(f for f in mem._all_facts() if f.object == "SyntheticCo")
    assert fact.id != "ft_source" and fact.user_id == "target"
    assert fact.valid_at == 0 and fact.created_at == -1
    ep = mem.episodes_doc.values()[0]
    assert fact.source == "user" and fact.provenance == [ep.id]
    assert ep.id != "ep_source" and ep.event_time == 0 and ep.consolidated
    after = contents(mem)
    for key in ("focus", "policy", "aliases"):
        assert after[key] == before[key]


def test_new_successor_links_to_previously_imported_duplicate():
    mem = MemoryEngine()
    first = snapshot()
    first["episodes"] = []
    mem.import_snapshot(first)
    predecessor = mem._all_facts()[0]
    second = copy.deepcopy(first)
    newer = copy.deepcopy(first["facts"][0])
    newer.update(id="ft_successor", object="NewSyntheticCo", valid_at=1, supersedes=["ft_source"])
    second["facts"].append(newer)
    stats = mem.import_snapshot(second)
    successor = next(f for f in mem._all_facts() if f.object == "NewSyntheticCo")
    assert stats["facts_skipped"] == 1
    assert successor.supersedes == [predecessor.id]


def test_embedding_failure_does_not_apply_prepared_records(monkeypatch):
    mem = target()
    before = contents(mem)
    payload = snapshot()
    payload["episodes"][0]["digest"] = "Source digest"
    calls = 0
    embed = mem.embedder.embed
    def fail_on_fact(text):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic embedding failure")
        return embed(text)
    monkeypatch.setattr(mem.embedder, "embed", fail_on_fact)
    with pytest.raises(RuntimeError, match="synthetic embedding failure"):
        mem.import_snapshot(payload)
    assert contents(mem) == before


def test_invalid_recomputed_vector_does_not_apply_prepared_episodes(monkeypatch):
    mem = target()
    before = contents(mem)
    payload = snapshot()
    payload["episodes"][0]["digest"] = "Source digest"
    embed = mem.embedder.embed
    calls = 0
    def malformed_fact_vector(text):
        nonlocal calls
        calls += 1
        return [float("nan")] if calls == 2 else embed(text)
    monkeypatch.setattr(mem.embedder, "embed", malformed_fact_vector)
    with pytest.raises(ValueError, match="embedding"):
        mem.import_snapshot(payload)
    assert contents(mem) == before


@pytest.mark.parametrize("revisions", [
    {}, [{"changed_at": 1}],
    [{"changed_at": True, "invalid_at": None, "expired_at": None}],
    [{"changed_at": 2, "invalid_at": None, "expired_at": None},
     {"changed_at": 1, "invalid_at": None, "expired_at": None}],
])
def test_invalid_validity_revisions_reject_before_partial_import(revisions):
    payload = snapshot()
    payload["facts"][0]["validity_revisions"] = revisions
    mem = target()
    before = contents(mem)
    with pytest.raises(ValueError):
        mem.import_snapshot(payload)
    assert contents(mem) == before
