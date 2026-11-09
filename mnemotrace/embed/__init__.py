"""Optional embedding backends are imported on demand."""
from .base import Embedder, EmbeddingUnavailable

__all__ = ["Embedder", "EmbeddingUnavailable", "FastEmbedEmbedder", "SentenceTransformerEmbedder", "LiteLLMEmbedder"]


def __getattr__(name: str):  # PEP 562 惰性子模块属性
    if name == "FastEmbedEmbedder":
        from .fastembed_embed import FastEmbedEmbedder

        return FastEmbedEmbedder
    if name == "SentenceTransformerEmbedder":
        from .sentence_transformers_embed import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder
    if name == "LiteLLMEmbedder":
        from .litellm_embed import LiteLLMEmbedder

        return LiteLLMEmbedder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
