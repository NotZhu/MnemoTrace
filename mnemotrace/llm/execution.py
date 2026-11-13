"""One bounded provider attempt and usage accounting for the write pipeline."""
from copy import copy
import time
from .base import LLM, complete_result

def one_attempt(llm):
    from .litellm_llm import LiteLLMClient
    from .cache import CachedLLM
    if isinstance(llm, (CachedLLM, ModelMeter)):
        llm = copy(llm)
        llm.inner = one_attempt(llm.inner)
    if isinstance(llm, LiteLLMClient):
        llm = copy(llm)
        llm.max_attempts = 1
        llm.kwargs = {**llm.kwargs, 'num_retries': 0}
    return llm


class ModelMeter(LLM):
    """Measure attempts at the call boundary, including rejected or discarded completions."""
    def __init__(self, inner, metrics, prefix='pipeline'):
        self.inner, self.metrics, self.prefix = inner, metrics, prefix

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
