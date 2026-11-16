"""One bounded provider attempt and usage accounting for the write pipeline."""
from copy import copy
import time
from .base import LLM, complete_result

def one_attempt(llm):
    """将支持重试的 LiteLLM 调用限制为一次尝试。

    Args:
        llm: 生成任务使用的模型，可带缓存或计量包装器。

    Returns:
        可供任务执行器调用的模型。
    """
    from .litellm_llm import LiteLLMClient
    from .cache import CachedLLM
    if isinstance(llm, (CachedLLM, ModelMeter)):
        inner = one_attempt(llm.inner)
        if inner is llm.inner:
            return llm
        llm = copy(llm)
        llm.inner = inner
    if isinstance(llm, LiteLLMClient):
        llm = copy(llm)
        llm.max_attempts = 1
        llm.kwargs = {**llm.kwargs, 'num_retries': 0}
    return llm


def discard_cached_completion(llm, prompt, completion, **kwargs):
    """丢弃未通过校验的模型生成缓存。

    Args:
        llm: 生成所使用的模型，可带缓存或计量包装器。
        prompt: 对应的用户提示。
        completion: 要丢弃的生成结果。
        **kwargs: 参与缓存键计算的生成选项。
    """
    from .cache import CachedLLM

    while isinstance(llm, (CachedLLM, ModelMeter)):
        if isinstance(llm, CachedLLM):
            llm.discard_completion(prompt, completion, **kwargs)
        llm = llm.inner


class ModelMeter(LLM):
    """Measure attempts at the call boundary, including rejected or discarded completions."""
    def __init__(self, inner, metrics, prefix='pipeline'):
        self.inner, self.metrics, self.prefix = inner, metrics, prefix

    @property
    def context_window(self):
        return getattr(self.inner, "context_window", None)

    def require_token_counter(self):
        from .tokens import require_counter

        require_counter(self.inner)

    def count_tokens(self, messages):
        return self.inner.count_tokens(messages)

    def complete(self, prompt, **kwargs):
        return self.complete_result(prompt, **kwargs).text

    def complete_result(self, prompt, **kwargs):
        started, result = time.perf_counter(), None
        try:
            result = complete_result(self.inner, prompt, **kwargs)
            return result
        finally:
            self.metrics.observe(self.prefix + '_model_call', time.perf_counter() - started)
            usage = result.usage if result is not None else None
            for name, count in (usage or {}).items():
                self.metrics.count(self.prefix + '_' + name, count)
            if usage is None or 'total_tokens' not in usage:
                self.metrics.count(self.prefix + '_usage_unreported')
