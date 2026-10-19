"""确定性、不联网的 LLM，用于测试和 demo。可以用 handler(prompt, system)->str 驱动，
或者给一组固定的回复。会记录所有调用以便断言。"""
from __future__ import annotations

from typing import Callable, Optional

from .base import LLM


class FakeLLM(LLM):
    def __init__(
        self,
        handler: Optional[Callable[[str, Optional[str]], str]] = None,
        responses: Optional[list[str]] = None,
    ) -> None:
        self._handler = handler
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:  # noqa: ARG002
        self.calls.append({"prompt": prompt, "system": system})
        if self._handler is not None:
            return self._handler(prompt, system)
        if self._responses:
            return self._responses.pop(0)
        return ""
