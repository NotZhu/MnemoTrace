"""Storage selection and path ownership must not depend on optional packages."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from mnemotrace import Config

from tests.model_fixtures import Memory
from mnemotrace.store.memory_store import InMemoryVectorStore


@pytest.mark.parametrize(
    ("entrypoint", "data_path"),
    [("construct", None), ("construct", ""), ("open", "")],
)
def test_lancedb_requires_path_before_import_or_directory_creation(
    monkeypatch, tmp_path, entrypoint, data_path,
):
    def forbidden_temp_directory(*args, **kwargs):
        pytest.fail("Memory must not allocate an implicit temporary directory")

    monkeypatch.setattr(tempfile, "mkdtemp", forbidden_temp_directory)
    monkeypatch.setitem(sys.modules, "mnemotrace.store.lancedb_store", None)
    cfg = Config(storage="lancedb", data_path=data_path)

    with pytest.raises(ValueError, match="lancedb.*non-empty data_path"):
        if entrypoint == "construct":
            Memory(config=cfg)
        else:
            Memory.open(str(tmp_path / "snapshot"), config=cfg)

    assert cfg.data_path == data_path
    assert not list(tmp_path.iterdir())


@pytest.fixture
def vector_store_calls(monkeypatch):
    calls = []

    def forbidden_temp_directory(*args, **kwargs):
        pytest.fail("Memory must not allocate an implicit temporary directory")

    monkeypatch.setattr(tempfile, "mkdtemp", forbidden_temp_directory)

    class RecordingVectorStore(InMemoryVectorStore):
        def __init__(self, path, table):
            super().__init__()
            calls.append((Path(path), table))

    monkeypatch.setattr(
        "mnemotrace.store.lancedb_store.LanceDBVectorStore", RecordingVectorStore,
    )
    return calls


@pytest.mark.parametrize("entrypoint", ["construct", "open"])
def test_explicit_lancedb_path_is_used_for_every_index(
    tmp_path, vector_store_calls, entrypoint,
):
    path = tmp_path / "vectors"
    cfg = Config(storage="lancedb", data_path=str(path))
    if entrypoint == "construct":
        Memory(config=cfg)
    else:
        Memory.open(str(tmp_path / "snapshot"), config=cfg)

    assert vector_store_calls == [
        (path, name)
        for name in ("episodes_vec", "fact_store", "cold_store", "summary_vec")
    ]
    assert cfg.data_path == str(path)


@pytest.mark.parametrize("explicit_default_factory", [False, True])
def test_open_derives_isolated_lancedb_paths_without_changing_config(
    tmp_path, vector_store_calls, explicit_default_factory,
):
    cfg = Config(storage="lancedb")
    kwargs = {"vector_store_factory": InMemoryVectorStore} if explicit_default_factory else {}
    alice = Memory.open(str(tmp_path / "alice"), config=cfg, **kwargs)
    bob = Memory.open(str(tmp_path / "bob"), config=cfg, **kwargs)

    assert [path for path, _ in vector_store_calls] == [
        tmp_path / "alice" / "lancedb",
    ] * 4 + [tmp_path / "bob" / "lancedb"] * 4
    assert alice.config.data_path == f"{tmp_path / 'alice'}/lancedb"
    assert bob.config.data_path == f"{tmp_path / 'bob'}/lancedb"
    assert cfg.data_path is None


@pytest.mark.parametrize("entrypoint", ["construct", "open"])
def test_custom_vector_factory_owns_its_location(monkeypatch, tmp_path, entrypoint):
    monkeypatch.setitem(sys.modules, "mnemotrace.store.lancedb_store", None)
    cfg = Config(storage="lancedb")
    kwargs = {"config": cfg, "vector_store_factory": lambda: InMemoryVectorStore()}
    mem = Memory(**kwargs) if entrypoint == "construct" else Memory.open(
        str(tmp_path / "snapshot"), **kwargs,
    )

    assert all(isinstance(store, InMemoryVectorStore) for store in (
        mem.episodes_vec, mem.fact_store, mem.cold_store, mem.summary_vec,
    ))
    assert mem.config is cfg
    assert cfg.data_path is None


@pytest.mark.parametrize("kwargs", [
    {}, {"data_path": ""},
    {"qdrant_url": "http://127.0.0.1:6333"},
    {"qdrant_url": "http://127.0.0.1:6333", "data_path": "vectors", "qdrant_namespace": "u"},
    {"data_path": "vectors", "qdrant_namespace": ""},
    {"data_path": "vectors", "qdrant_collection_prefix": "../unsafe"},
    {"data_path": "vectors", "qdrant_timeout": 0},
    {"qdrant_url": "https://secret@example.com", "qdrant_namespace": "u"},
    {"qdrant_url": "http://example.com", "qdrant_namespace": "u"},
])
def test_invalid_qdrant_config_fails_before_optional_import(monkeypatch, kwargs):
    monkeypatch.setitem(sys.modules, "qdrant_client", None)
    with pytest.raises(ValueError):
        Memory(config=Config(storage="qdrant", **kwargs))


def test_qdrant_custom_factory_does_not_import_or_validate_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "qdrant_client", None)
    mem = Memory(config=Config(storage="qdrant"), vector_store_factory=lambda: InMemoryVectorStore())
    assert isinstance(mem.fact_store, InMemoryVectorStore)
    mem.close()


def test_default_import_and_config_repr_do_not_expose_qdrant_key():
    import subprocess
    result = subprocess.run([sys.executable, "-c",
        "import sys; from mnemotrace import Memory; Memory(); "
        "assert 'qdrant_client' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "synthetic-secret" not in repr(Config(qdrant_api_key="synthetic-secret"))
