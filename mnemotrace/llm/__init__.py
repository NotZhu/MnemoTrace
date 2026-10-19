"""LLM 后端。OfflineLLM + FakeLLM 立即加载（无依赖）；LiteLLMClient 是惰性的，
确保 `import mnemotrace` 永远不会导入 litellm。"""
from .base import LLM
from .fake import FakeLLM
from .offline import OfflineLLM

__all__ = ["LLM", "OfflineLLM", "FakeLLM", "LiteLLMClient"]


def __getattr__(name: str):
    if name == "LiteLLMClient":
        from .litellm_llm import LiteLLMClient

        return LiteLLMClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
