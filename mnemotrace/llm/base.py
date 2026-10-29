"""LLM 接口。生产环境的抽取/裁决/摘要经由真实模型完成（通过 LiteLLM：Kimi、DeepSeek、GPT、Qwen、
本地模型等）。抽取与自动生命周期判断必须使用 LLM；离线回归使用固定模型响应。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Completion:
    """Text plus provider termination and token counts; None means unreported."""

    text: str
    finish_reason: str | None = None
    usage: dict[str, int] | None = None

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise ValueError("Completion text must be a string")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise ValueError("Invalid completion finish reason")
        if self.usage is not None and (
            not isinstance(self.usage, dict) or any(
                k not in {"prompt_tokens", "completion_tokens", "total_tokens"}
                or type(v) is not int or v < 0 for k, v in self.usage.items())
        ):
            raise ValueError("Invalid completion token usage")


def complete_result(llm, prompt: str, **kwargs) -> Completion:
    """Plain text adapters remain usable, without inventing termination metadata."""
    method = getattr(llm, "complete_result", None)
    result = method(prompt, **kwargs) if method else Completion(llm.complete(prompt, **kwargs))
    if not isinstance(result, Completion):
        raise ValueError("complete_result must return Completion")
    return result


class LLM(ABC):
    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> str: ...

    def complete_result(self, prompt: str, **kwargs) -> Completion:
        return Completion(self.complete(prompt, **kwargs))
