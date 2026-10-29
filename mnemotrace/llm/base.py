"""LLM 接口。生产环境的抽取/裁决/摘要经由真实模型完成（通过 LiteLLM：Kimi、DeepSeek、GPT、Qwen、
本地模型等）。抽取与自动生命周期判断必须使用 LLM；离线回归使用固定模型响应。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLM(ABC):
    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> str: ...
