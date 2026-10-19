"""基于 LiteLLM 的 LLM 后端 —— 一个接口通吃 OpenAI、Anthropic、DeepSeek、Moonshot/Kimi、Gemini、
Qwen、OpenRouter、本地服务器等。需要在环境中配置对应 provider 的 key。"""
from __future__ import annotations

import os

from typing import Optional

from .base import LLM

# caption() 的默认指令：一段密集的、利于检索的描述（图中可见的名字、文字、数字）——
# 一张原本无法检索的图片由此变成可搜索的记忆。
_CAPTION_PROMPT = (
    "Describe this image for long-term memory. Be specific and concise: name the key objects, people, "
    "places, and any visible text or numbers. One short paragraph, no preamble."
)


class LiteLLMClient(LLM):
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

    # 小写化的子串，用于把错误判定为"永久性"——配置/认证/请求问题，重试解决不了。其余一切
    # （连接重置、超时、SSL EOF、429/5xx、"overloaded"）都按瞬时错误重试，因为经验上摧毁并行
    # 中转运行的基本全是这些。
    _PERMANENT = (
        "authenticationerror", "invalid api key", "invalid_api_key", "incorrect api key",
        "permissiondenied", "permission denied", "no api key", "api key not",
    )

    def _run(self, messages: list, **kwargs) -> str:
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
        # 在 litellm 自身的 num_retries 之上再套一层外层退避循环。中转（如 univibe）和 provider API
        # （deepseek）在并发下会掉连接；litellm 的进程内快速重试在中转恢复之前就已耗尽。此前这个
        # 循环是 4 次尝试 / 线性 1.5s 退避，一次 500 题、6 worker 的运行仍然因瞬时错误丢了约 53% 的
        # 题目 —— 整个基准作废。现在：更多尝试次数、带 full jitter 的指数退避（打破 worker 间的
        # 雷群锁步）、对空内容也重试（中转有时以 200 返回空 body），并且只对真正永久性的认证错误
        # 快速失败，避免在配置错误上烧掉预算。
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                resp = self._litellm.completion(**params)
                content = resp.choices[0].message.content
                if content:
                    return content
                # 不稳定中转返回空 body：按瞬时错误处理，而不是静默返回 ""。
                last_exc = RuntimeError("empty completion content")
            except Exception as e:  # noqa: BLE001 - 先分类：瞬时的重试 / 永久性的快速失败
                last_exc = e
                msg = f"{type(e).__name__}: {e}".lower()
                if any(p in msg for p in self._PERMANENT):
                    raise
            if attempt < self.max_attempts - 1:
                delay = min(self.retry_base * (2 ** attempt), self.retry_cap)
                time.sleep(delay * (0.5 + random.random()))  # full jitter：0.5x..1.5x
        raise last_exc  # type: ignore[misc]

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
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
        return self._run([{"role": "user", "content": content}])
