"""基于 LiteLLM 的 LLM 后端 —— 一个接口通吃 OpenAI、Anthropic、DeepSeek、Moonshot/Kimi、Gemini、
Qwen、OpenRouter、本地服务器等。需要在环境中配置对应 provider 的 key。"""
from __future__ import annotations

import os

from typing import Optional

from .base import Completion, LLM
from .failures import retryable

# caption() 的默认指令：一段密集的、利于检索的描述（图中可见的名字、文字、数字）——
# 一张原本无法检索的图片由此变成可搜索的记忆。
_CAPTION_PROMPT = (
    "Describe this image for long-term memory. Be specific and concise: name the key objects, people, "
    "places, and any visible text or numbers. One short paragraph, no preamble."
)


class LiteLLMClient(LLM):
    """LiteLLM 的客户端封装。"""
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        max_attempts: int = 7,
        retry_base: float = 1.0,
        retry_cap: float = 30.0,
        **kwargs,
    ) -> None:
        """初始化 LiteLLM 客户端。

        Args:
            model: 模型名称。
            temperature: 采样温度。
            max_tokens: 最大生成 token 数。
            max_attempts: 最大尝试次数。
            retry_base: 重试的基础延迟（秒）。
            retry_cap: 重试的最大延迟（秒）。
            **kwargs: 其他传递给 LiteLLM 的参数。
        """
        import litellm  # 延迟导入

        self._litellm = litellm
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # 外层重试的韧性旋钮。与 litellm 自身的 `num_retries`（留在 kwargs 里，负责进程内快速重试）
        # 分开：这几个参数控制的是慢速的中转恢复退避。
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.retry_cap = retry_cap
        self.kwargs = kwargs

    def _run(self, messages: list, **kwargs) -> Completion:
        """在现成的 `messages` 列表上调用模型，带韧性退避循环。文本的 `complete()` 和多模态的
        `caption()` 共用它，从而获得相同的重试/快速失败行为。"""
        import random
        import time

        params = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            # 单次调用硬超时：不设的话，一个接受了 TCP 连接然后沉默的 provider（夜间运行中观察到
            # 两次）会让所有 worker 永远挂着 —— 下面的重试循环根本没机会跑。对长推理回答足够宽裕。
            timeout=float(os.environ.get("MNEMOTRACE_LLM_TIMEOUT", "180")),
        )
        params.update(self.kwargs)
        params.update(kwargs)
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                resp = self._litellm.completion(**params)
                choice = resp.choices[0]
                content = choice.message.content
                reason = getattr(choice, "finish_reason", None)
                if content is not None or reason not in (None, "stop", "end_turn"):
                    counts = getattr(resp, "usage", None)
                    usage = {k: v for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                             if type(v := getattr(counts, k, None)) is int and v >= 0}
                    return Completion(content or "", reason, usage or None)
                # 不稳定中转返回空 body：按瞬时错误处理，而不是静默返回 ""。
                last_exc = RuntimeError("empty completion content")
            except Exception as e:  # noqa: BLE001 - 先分类：瞬时的重试 / 永久性的快速失败
                last_exc = e
                if not retryable(e):
                    raise
            if attempt < self.max_attempts - 1:
                delay = min(self.retry_base * (2 ** attempt), self.retry_cap)
                time.sleep(delay * (0.5 + random.random()))  # full jitter：0.5x..1.5x
        raise last_exc  # type: ignore[misc]

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        return self.complete_result(prompt, system=system, **kwargs).text

    def complete_result(self, prompt: str, system: Optional[str] = None, **kwargs) -> Completion:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._run(messages, **kwargs)

    def caption(self, image_ref: str, prompt: Optional[str] = None) -> str:
        """把图片（data: URL 或 http(s) URL）描述成纯文本 —— 多模态路径。调用方只把图片字节/URL
        路由到这里；非视觉模型会直接报错，调用方随后回退到占位符。使用所有视觉 provider 都接受的
        同一种 OpenAI 多模态 content 结构。"""
        content = [
            {"type": "text", "text": prompt or _CAPTION_PROMPT},
            {"type": "image_url", "image_url": {"url": image_ref}},
        ]
        result = self._run([{"role": "user", "content": content}])
        if result.finish_reason not in (None, "stop", "end_turn"):
            raise RuntimeError("Image description did not complete")
        return result.text
