"""不联网的 LLM 占位实现。它并不真正推理；存在的意义是让那些*可选*调用 LLM 的代码路径
在没有配置模型时不崩溃。整合环节真正的离线行为在规则组件里，不在这里。"""
from __future__ import annotations

from .base import LLM


class OfflineLLM(LLM):
    def complete(self, prompt: str, **kwargs) -> str:  # noqa: ARG002
        return ""
