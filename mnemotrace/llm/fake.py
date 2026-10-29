"""确定性、不联网的 LLM，用于固定响应测试。可以用 handler(prompt, system)->str 驱动，
或者给一组固定的回复。会记录所有调用以便断言。"""
from __future__ import annotations

from typing import Callable, Optional

from .base import Completion, LLM


class FakeLLM(LLM):
    def __init__(
        self,
        handler: Optional[Callable[[str, Optional[str]], str | Completion]] = None,
        responses: Optional[list[str | Completion]] = None,
    ) -> None:
        self._handler = handler
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:  # noqa: ARG002
        return self.complete_result(prompt, system=system, **kwargs).text

    def complete_result(self, prompt: str, system: Optional[str] = None, **kwargs) -> Completion:
        self.calls.append({"prompt": prompt, "system": system})
        if self._handler is not None:
            reply = self._handler(prompt, system)
        else:
            reply = self._responses.pop(0) if self._responses else ""
        return reply if isinstance(reply, Completion) else Completion(reply)
