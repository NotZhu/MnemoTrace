from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from mnemotrace.embed.litellm_embed import LiteLLMEmbedder
from mnemotrace.llm.providers import make_embedder


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
