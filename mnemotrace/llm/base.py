"""LLM 接口。生产环境的抽取/裁决/摘要经由真实模型完成（通过 LiteLLM：Kimi、DeepSeek、GPT、Qwen、
本地模型等）。离线时，整合回退到确定性规则，因此 LLM 对 demo 和测试是可选的。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLM(ABC):
    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> str: ...
