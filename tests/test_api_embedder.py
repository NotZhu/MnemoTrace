from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemotrace.embed.litellm_embed import LiteLLMEmbedder
from mnemotrace.llm.providers import make_embedder
from tests.model_fixtures import MemoryService
from mnemotrace.store import DimensionMismatchError, EmbedderMismatchError


@pytest.fixture
def embedding_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []

    def embedding(*, model, input):
        calls.append((model, input))
        return SimpleNamespace(data=[{"embedding": [1.0] + [0.0] * 1023} for _ in input])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    return calls


@pytest.mark.parametrize("model", ["BAAI/bge-m3", "openai/BAAI/bge-m3"])
def test_bge_m3_dimension_is_known_before_api_call(embedding_api, model):
    embedder = LiteLLMEmbedder(model)
    assert embedder.dim == 1024
    assert embedding_api == []
    assert len(embedder.embed("synthetic memory")) == 1024
    assert embedder.dim == 1024


def test_factory_keeps_openai_compatible_bge_model(embedding_api):
    embedder = make_embedder("openai:openai/BAAI/bge-m3")
    assert embedder.model == "openai/BAAI/bge-m3"
    assert embedder.dim == 1024
    embedder.embed("synthetic memory")
    assert embedding_api == [("openai/BAAI/bge-m3", ["synthetic memory"])]


@pytest.mark.parametrize("model", ["text-embedding-3-small", "openai/text-embedding-3-small"])
def test_other_api_models_require_explicit_dimension(embedding_api, model):
    with pytest.raises(ValueError, match="explicit dim"):
        LiteLLMEmbedder(model)
    assert embedding_api == []


def test_default_api_model_is_bge_m3_before_and_after_first_response(embedding_api):
    embedder = LiteLLMEmbedder()
    assert embedder.model == "openai/BAAI/bge-m3"
    assert embedder.dim == 1024
    assert embedding_api == []
    assert len(embedder.embed("synthetic memory")) == 1024
    assert embedder.dim == 1024
    assert embedding_api == [("openai/BAAI/bge-m3", ["synthetic memory"])]


def test_factory_uses_bge_m3_when_api_model_is_omitted(embedding_api):
    embedder = make_embedder("openai:")
    assert embedder.model == "openai/BAAI/bge-m3"
    assert embedder.dim == 1024
    assert embedding_api == []


@pytest.mark.parametrize("dim", [512, 1536])
def test_bge_m3_rejects_incompatible_configured_dimension(embedding_api, dim):
    with pytest.raises(ValueError, match="1024"):
        make_embedder("openai:openai/BAAI/bge-m3", dim=dim)
    assert embedding_api == []


@pytest.mark.parametrize("dim", [0, -1, True, 1024.0, "1024"])
def test_invalid_dimension_is_rejected_before_api_call(embedding_api, dim):
    with pytest.raises(ValueError, match="positive integer"):
        LiteLLMEmbedder(dim=dim)
    assert embedding_api == []


def test_other_model_can_use_explicit_dimension_without_changing_model(embedding_api):
    embedder = make_embedder("openai:synthetic-1024", dim=1024)
    assert embedder.model == "synthetic-1024"
    assert embedder.dim == 1024
    assert len(embedder.embed("synthetic memory")) == 1024
    assert embedding_api == [("synthetic-1024", ["synthetic memory"])]


@pytest.mark.parametrize("dimensions", [(1536,), (1024, 1536)])
def test_response_dimension_mismatch_does_not_change_declared_dimension(
    embedding_api, monkeypatch, dimensions
):
    embedder = LiteLLMEmbedder("openai/BAAI/bge-m3")

    def wrong_dimension(**kwargs):
        return SimpleNamespace(data=[{"embedding": [0.0] * dim} for dim in dimensions])

    monkeypatch.setattr(embedder._litellm, "embedding", wrong_dimension)
    with pytest.raises(ValueError, match="dimension"):
        embedder.embed_batch(["synthetic memory"] * len(dimensions))
    assert embedder.dim == 1024


@pytest.mark.parametrize("model", ["openai:", "openai:openai/BAAI/bge-m3"])
def test_bge_service_reopens_without_api_calls_or_reembedding(
    embedding_api, monkeypatch, tmp_path, model
):
    monkeypatch.setenv("MNEMOTRACE_ANSWERER", "")
    monkeypatch.setenv("MNEMOTRACE_VISION_LLM", "")
    monkeypatch.delenv("MNEMOTRACE_REEMBED_ON_MISMATCH", raising=False)
    service = MemoryService(data_dir=str(tmp_path), embedder_name=model, llm_name="")
    service.remember("user123", "I live in Shenzhen.")
    manifest_path = Path(service._path("user123")) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["embedding_dim"] == 1024
    assert embedding_api
    episode_count = len(service.get("user123").episodes_doc.values())
    embedding_api.clear()

    restarted = MemoryService(data_dir=str(tmp_path), embedder_name=model, llm_name="")
    memory = restarted.get("user123")
    assert len(memory.episodes_doc.values()) == episode_count
    assert memory.fact_store.values()
    assert all(len(fact.embedding) == 1024 for fact in memory.fact_store.values())
    assert embedding_api == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [("embedding_dim", 1536, DimensionMismatchError),
     ("embedder_id", "another-model", EmbedderMismatchError)],
)
def test_bge_reopen_still_rejects_incompatible_store(
    embedding_api, monkeypatch, tmp_path, field, value, error
):
    monkeypatch.setenv("MNEMOTRACE_ANSWERER", "")
    monkeypatch.setenv("MNEMOTRACE_VISION_LLM", "")
    monkeypatch.delenv("MNEMOTRACE_REEMBED_ON_MISMATCH", raising=False)
    model = "openai:openai/BAAI/bge-m3"
    service = MemoryService(data_dir=str(tmp_path), embedder_name=model, llm_name="")
    service.remember("user123", "I live in Shenzhen.")
    manifest_path = Path(service._path("user123")) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    embedding_api.clear()

    restarted = MemoryService(data_dir=str(tmp_path), embedder_name=model, llm_name="")
    with pytest.raises(error):
        restarted.get("user123")
    assert embedding_api == []
