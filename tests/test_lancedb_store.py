from __future__ import annotations

import json
import sys
from dataclasses import asdict

import pytest

pytest.importorskip("lancedb")

from mnemotrace.config import Config
from mnemotrace.memory import Memory
from mnemotrace.store.lancedb_store import LanceDBVectorStore
from mnemotrace.store.memory_store import InMemoryVectorStore
from mnemotrace.types import Fact
from mnemotrace.store import StoreFormatError, load_memory


def _fact(subject: str, predicate: str, obj: str, vec: list[float]) -> Fact:
    f = Fact(subject=subject, predicate=predicate, object=obj, user_id="u", embedding=vec)
    return f


def test_lancedb_where_filter_matches_in_memory_when_hits_are_far(tmp_path):
    mem_store = InMemoryVectorStore()
    lance = LanceDBVectorStore(str(tmp_path / "lancedb"), "facts")
    query = [1.0, 0.0]

    for i in range(12):
        other = Fact(
            subject="other",
            predicate="near",
            object=str(i),
            user_id="other",
            embedding=[1.0, 0.0],
        )
        mem_store.upsert(other.id, other.embedding, other)
        lance.upsert(other.id, other.embedding, other)

    target = _fact("user", "likes", "bananas", [0.0, 1.0])
    mem_store.upsert(target.id, target.embedding, target)
    lance.upsert(target.id, target.embedding, target)

    expected = [f.id for _, f in mem_store.search(query, 1, where=lambda f: f.user_id == "u")]
    actual = [f.id for _, f in lance.search(query, 1, where=lambda f: f.user_id == "u")]
    assert actual == expected == [target.id]


def test_lancedb_vector_store_persists_across_restart(tmp_path):
    store = LanceDBVectorStore(str(tmp_path / "lancedb"), "facts")
    fact = _fact("user", "works_at", "Moonshot AI", [1.0, 0.0])
    store.upsert(fact.id, fact.embedding, fact)

    reopened = LanceDBVectorStore(str(tmp_path / "lancedb"), "facts")
    hit = reopened.get(fact.id)
    assert isinstance(hit, Fact)
    assert hit.id == fact.id and hit.object == "Moonshot AI"
    assert reopened.search([1.0, 0.0], 1)[0][1].id == fact.id


def test_lancedb_topk_matches_in_memory_for_normalized_vectors(tmp_path):
    facts = [
        _fact("user", "likes", "Python", [1.0, 0.0, 0.0]),
        _fact("user", "likes", "Rust", [0.0, 1.0, 0.0]),
        _fact("user", "likes", "TypeScript", [0.0, 0.0, 1.0]),
    ]
    mem_store = InMemoryVectorStore()
    lance = LanceDBVectorStore(str(tmp_path / "lancedb"), "facts")
    for f in facts:
        mem_store.upsert(f.id, f.embedding, f)
        lance.upsert(f.id, f.embedding, f)

    query = [0.9, 0.1, 0.0]
    expected = [f.id for _, f in mem_store.search(query, 2, where=lambda f: f.user_id == "u")]
    actual = [f.id for _, f in lance.search(query, 2, where=lambda f: f.user_id == "u")]
    assert actual == expected


def test_memory_config_lancedb_uses_lancedb_stores_and_reopens(tmp_path):
    cfg = Config(storage="lancedb", data_path=str(tmp_path / "vectors"))
    mem = Memory(config=cfg)
    mem.add_fact("user", "works_at", "Moonshot AI", user_id="u")
    mem.save(str(tmp_path / "snapshot"))
    manifest = json.loads((tmp_path / "snapshot" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["backend"] == "lancedb"

    reopened = Memory.open(str(tmp_path / "snapshot"), config=cfg)
    assert isinstance(reopened.fact_store, LanceDBVectorStore)
    assert "moonshot" in reopened.search("Where do I work?", user_id="u").answer().lower()


def test_lancedb_consolidation_persists_invalidated_fact_payload(tmp_path):
    cfg = Config(storage="lancedb", data_path=str(tmp_path / "vectors"))
    mem = Memory(config=cfg)
    mem.add("My name is Finn and I work at IBM.", user_id="u", event_time=1.0)
    mem.add("I now work at Oracle.", user_id="u", event_time=2.0)
    mem.consolidate()
    mem.save(str(tmp_path / "snapshot"))

    reopened = Memory.open(str(tmp_path / "snapshot"), config=cfg)
    history = reopened.history("Finn", "works_at", user_id="u")
    assert {f.object for f in history} == {"IBM", "Oracle"}
    assert [f.object for f in history if f.is_live()] == ["Oracle"]
    assert "oracle" in reopened.search("Where does Finn work?", user_id="u").answer().lower()


@pytest.mark.parametrize("explicit_default_factory", [False, True])
def test_lancedb_open_without_data_path_isolates_each_store(tmp_path, explicit_default_factory):
    cfg = Config(storage="lancedb")
    kwargs = {"vector_store_factory": InMemoryVectorStore} if explicit_default_factory else {}
    a = Memory.open(str(tmp_path / "alice"), config=cfg, **kwargs)
    b = Memory.open(str(tmp_path / "bob"), config=cfg, **kwargs)
    a.add_fact("user", "likes", "apples", user_id="alice")
    b.add_fact("user", "likes", "bananas", user_id="bob")
    a.save()
    b.save()

    alice = Memory.open(str(tmp_path / "alice"), config=cfg, **kwargs)
    bob = Memory.open(str(tmp_path / "bob"), config=cfg, **kwargs)
    assert [f.object for f in alice.fact_store.values()] == ["apples"]
    assert [f.object for f in bob.fact_store.values()] == ["bananas"]
    assert "apples" in alice.search("What do I like?", user_id="alice").answer().lower()
    assert "bananas" in bob.search("What do I like?", user_id="bob").answer().lower()
    assert (tmp_path / "alice" / "lancedb").exists()
    assert (tmp_path / "bob" / "lancedb").exists()
    assert cfg.data_path is None


def test_default_memory_path_does_not_import_lancedb(tmp_path):
    sys.modules.pop("lancedb", None)
    mem = Memory()
    mem.add("I like Python.", user_id="u")
    mem.save(str(tmp_path / "snapshot"))
    assert "lancedb" not in sys.modules


@pytest.mark.parametrize("damage", ["state", "last_record"])
def test_invalid_restore_does_not_change_existing_lancedb_rows(tmp_path, damage):
    mem = Memory(config=Config(storage="lancedb", data_path=str(tmp_path / "vectors")))
    mem.add_fact("Existing", "likes", "tea", user_id="u")
    table = mem.fact_store._open()
    before = table.to_arrow().to_pylist()
    source = Memory()
    source.add_fact("Source", "likes", "coffee", user_id="u")
    source.add_fact("Source", "works_at", "SyntheticCo", user_id="u")
    path = tmp_path / "snapshot"
    source.save(str(path))
    if damage == "state":
        target = path / "manifest.json"
        manifest = json.loads(target.read_text(encoding="utf-8"))
        manifest["state"]["import_seen"] = 42
        target.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        target = path / "facts.jsonl"
        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        rows[-1].pop("expired_at")
        target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(StoreFormatError):
        load_memory(mem, str(path))
    assert table.to_arrow().to_pylist() == before


def test_corrupt_typed_lancedb_payload_fails_without_repairing_table(tmp_path):
    store = LanceDBVectorStore(str(tmp_path / "vectors"), "facts")
    fact = _fact("Synthetic", "likes", "tea", [1.0, 0.0])
    row = asdict(fact)
    row.pop("id")
    store.upsert("ft_synthetic", fact.embedding, fact)
    table = store._open()
    table.delete("key = 'ft_synthetic'")
    table.add([{"key": "ft_synthetic", "vector": fact.embedding,
                "payload": json.dumps({"type": "Fact", "data": row})}])
    before = table.to_arrow().to_pylist()
    with pytest.raises(StoreFormatError, match="id"):
        store.get("ft_synthetic")
    assert table.to_arrow().to_pylist() == before


def test_invalid_snapshot_import_does_not_write_lancedb_rows(tmp_path):
    mem = Memory(config=Config(storage="lancedb", data_path=str(tmp_path / "vectors")))
    mem.add_fact("Existing", "likes", "tea")
    before = mem.fact_store._open().to_arrow().to_pylist()
    valid = asdict(Fact(id="ft_valid", subject="Source", predicate="likes", object="coffee"))
    invalid = {**valid, "id": "ft_invalid", "provenance": 42}
    with pytest.raises(ValueError, match="provenance"):
        mem.import_snapshot({"mnemotrace_export_version": 2, "facts": [valid, invalid], "episodes": []})
    assert mem.fact_store._open().to_arrow().to_pylist() == before


def test_corrupt_existing_later_index_does_not_clear_earlier_index(tmp_path):
    mem = Memory(config=Config(storage="lancedb", data_path=str(tmp_path / "vectors")))
    mem.add("Synthetic source", user_id="u")
    fact = mem.add_fact("Synthetic", "likes", "tea", user_id="u")
    before = mem.episodes_vec._open().to_arrow().to_pylist()
    row = asdict(fact)
    row.pop("id")
    table = mem.fact_store._open()
    table.delete("key = '" + fact.id + "'")
    table.add([{"key": fact.id, "vector": fact.embedding,
                "payload": json.dumps({"type": "Fact", "data": row})}])
    source = Memory()
    source.add("Replacement source", user_id="u")
    source.save(str(tmp_path / "snapshot"))
    with pytest.raises(StoreFormatError, match="id"):
        load_memory(mem, str(tmp_path / "snapshot"))
    assert mem.episodes_vec._open().to_arrow().to_pylist() == before
