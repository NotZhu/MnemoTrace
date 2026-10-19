"""嵌入器。离线的 HashingEmbedder 立即加载（零依赖）；真实后端以惰性方式暴露，
确保 `import mnemotrace` 永远不会拉起 torch / litellm。"""
from .base import Embedder
from .hashing import HashingEmbedder

__all__ = ["Embedder", "HashingEmbedder", "SentenceTransformerEmbedder", "LiteLLMEmbedder"]


def __getattr__(name: str):  # PEP 562 惰性子模块属性
    if name == "SentenceTransformerEmbedder":
        from .sentence_transformers_embed import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder
    if name == "LiteLLMEmbedder":
        from .litellm_embed import LiteLLMEmbedder

        return LiteLLMEmbedder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
