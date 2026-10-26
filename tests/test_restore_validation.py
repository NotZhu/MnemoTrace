"""Restore existing data without inventing identity, history or runtime state."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pytest

from mnemotrace import Memory
from mnemotrace.store import IncompatibleStoreError, StoreFormatError, load_memory
from mnemotrace.store.persist import snapshot_version
from mnemotrace.types import Conflict


@pytest.fixture()
def saved(tmp_path):
    mem = Memory()
    mem.add("Synthetic source", user_id="u", event_time=0)
    fact = mem.add_fact("Synthetic", "works_at", "SyntheticCo", user_id="u", valid_at=0)
    mem.remember_working("Synthetic task", user_id="u", session_id="s")
    mem.conflicts["cf_synthetic"] = Conflict(id="cf_synthetic", older=fact.id, newer=fact.id)
    mem.set_focus(track=["SyntheticCo"])
    mem.set_policy(extract_instruction="Synthetic policy")
    mem._identity = {"u": "Synthetic"}
    mem._aliases = {"u": {"Synthetic"}}
    mem._import_seen = {"synthetic-fingerprint"}
    path = tmp_path / "store"
    mem.save(str(path))
    return mem, path


def edit_manifest(path, change):
    target = path / "manifest.json"
    manifest = json.loads(target.read_text(encoding="utf-8"))
    change(manifest)
    target.write_text(json.dumps(manifest), encoding="utf-8")


def disk_bytes(path):
    return {p.name: p.read_bytes() for p in path.iterdir() if p.suffix in {".json", ".jsonl"}}


@pytest.mark.parametrize("key", [
    "schema_version", "snapshot_id", "embedder_id", "embedding_dim", "backend",
    "mnemotrace_version", "created_at", "updated_at", "counts", "state",
])
def test_required_manifest_fields_are_checked_by_load_and_version(saved, key):
    _, path = saved
    edit_manifest(path, lambda m: m.pop(key))
    before = disk_bytes(path)
    for read in (Memory.open, snapshot_version):
        with pytest.raises(StoreFormatError, match=key):
            read(str(path))
    assert disk_bytes(path) == before


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 1.9), ("schema_version", "1"),
    ("snapshot_id", ""), ("embedder_id", 1), ("embedding_dim", None),
    ("embedding_dim", True), ("embedding_dim", "256"), ("embedding_dim", 0),
    ("created_at", float("nan")), ("updated_at", float("inf")), ("backend", "unknown"),
])
def test_manifest_values_are_not_coerced(saved, key, value):
    _, path = saved
    edit_manifest(path, lambda m: m.update({key: value}))
    with pytest.raises(StoreFormatError, match=key):
        Memory.open(str(path), allow_mismatch=True)


@pytest.mark.parametrize("version", [0, -1, 1, 999])
def test_unsupported_version_takes_precedence_over_other_missing_fields(tmp_path, version):
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": version}), encoding="utf-8")
    with pytest.raises(IncompatibleStoreError):
        Memory.open(str(tmp_path))


@pytest.mark.parametrize("collection", ["episodes", "facts", "entities", "relations", "working", "conflicts"])
def test_each_persisted_record_field_is_checked_before_mutation(saved, collection):
    _, path = saved
    target = path / f"{collection}.jsonl"
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    original = target.read_bytes()
    existing = Memory()
    existing.add_fact("Existing", "likes", "ExistingCo")
    existing.set_focus(track=["existing"])
    before = [asdict(f) for f in existing.fact_store.values()]
    focus = copy.deepcopy(existing.focus)
    for key in rows[-1]:
        if collection == "facts" and key in {"text", "display"}:
            continue
        changed = copy.deepcopy(rows)
        del changed[-1][key]
        target.write_text("".join(json.dumps(row) + "\n" for row in changed), encoding="utf-8")
        with pytest.raises(StoreFormatError, match=key):
            load_memory(existing, str(path))
        assert [asdict(f) for f in existing.fact_store.values()] == before
        assert existing.focus == focus
        assert existing._snapshot_version is None
        target.write_bytes(original)


@pytest.mark.parametrize("key", ["focus", "policy", "identity", "aliases", "import_seen"])
def test_required_state_cannot_be_implicitly_reset(saved, key):
    _, path = saved
    edit_manifest(path, lambda m: m["state"].pop(key))
    with pytest.raises(StoreFormatError, match=key):
        Memory.open(str(path))


@pytest.mark.parametrize("key,value", [
    ("focus", {"track": [], "mute": "abc"}), ("focus", {"track": []}),
    ("policy", {}), ("identity", {"u": 1}), ("aliases", {"u": "abc"}),
    ("import_seen", "abc"), ("import_seen", 42), ("persona_cache", []),
    ("cold_pages_out", {"u": True}), ("cold_pages_in", {"u": -1}),
])
def test_invalid_state_fails_without_changing_memory(saved, key, value):
    original, path = saved
    edit_manifest(path, lambda m: m["state"].update({key: value}))
    before = [asdict(f) for f in original.fact_store.values()]
    settings = copy.deepcopy(original.focus)
    token = original._snapshot_version
    with pytest.raises(StoreFormatError, match=key):
        load_memory(original, str(path))
    assert [asdict(f) for f in original.fact_store.values()] == before
    assert original.focus == settings
    assert original._snapshot_version == token


def test_optional_caches_and_fact_display_can_be_omitted(saved):
    _, path = saved
    def remove_caches(m):
        for key in ("persona_cache", "cold_pages_in", "cold_pages_out"):
            m["state"].pop(key)
    edit_manifest(path, remove_caches)
    target = path / "facts.jsonl"
    row = json.loads(target.read_text(encoding="utf-8"))
    row.pop("text")
    row.pop("display")
    row["unknown_extension"] = "ignored"
    target.write_text(json.dumps(row) + "\n", encoding="utf-8")
    restored = Memory.open(str(path))
    assert restored.fact_store.values()[0].text == "Synthetic works at SyntheticCo"
    assert restored.fact_store.values()[0].valid_at == 0
    assert restored._persona_cache == {}


@pytest.mark.parametrize("change", ["record", "state", "manifest", "dimension", "encoding", "metadata"])
def test_save_preflights_before_replacing_any_file(saved, change):
    mem, path = saved
    if change == "record":
        mem.fact_store.values()[0].provenance = "not-a-list"
    elif change == "state":
        mem._import_seen = "not-a-set"
    elif change == "manifest":
        edit_manifest(path, lambda m: m.pop("snapshot_id"))
    elif change == "encoding":
        mem.fact_store.values()[0].display = "\ud800"
    elif change == "metadata":
        mem.episodes_doc.values()[0].metadata = {"value": float("nan")}
    else:
        mem.embedder.dim = True
    before = disk_bytes(path)
    token = mem._snapshot_version
    with pytest.raises(StoreFormatError):
        mem.save()
    assert disk_bytes(path) == before
    assert mem._snapshot_version == token


@pytest.mark.parametrize("key", ["invalid_at", "expired_at"])
def test_explicit_lifecycle_and_source_survive_restore(saved, key):
    mem, path = saved
    fact = mem.fact_store.values()[0]
    setattr(fact, key, 1)
    assert not fact.is_live()
    item = next(iter(mem.working_mem.values()))
    item.consumed = True
    item.expires_at = 1
    mem.save()
    restored = Memory.open(str(path))
    assert not restored.fact_store.values()[0].is_live()
    assert restored.fact_store.values()[0].source == "user"
    restored_item = next(iter(restored.working_mem.values()))
    assert restored_item.consumed and restored_item.expires_at == 1
    assert not restored_item.is_live()


def test_lancedb_typed_decoder_rejects_missing_identity_without_backend_dependency():
    from mnemotrace.store.lancedb_store import _decode_payload
    from mnemotrace.types import Fact
    row = asdict(Fact(subject="Synthetic", predicate="likes", object="tea"))
    row.pop("id")
    with pytest.raises(StoreFormatError, match="id"):
        _decode_payload(json.dumps({"type": "Fact", "data": row}))
