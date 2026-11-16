"""Local counting for the same chat messages sent by the model adapter."""
from importlib import import_module
import os
import json


class TokenCounterConfigurationError(ValueError):
    """The configured model cannot count the requests used for auxiliary decisions."""


class ModelContextConfigurationError(ValueError):
    """The model context capacity is missing or invalid."""


def conservative_count(chat):
    """Estimate an upper budget from the UTF-8 chat payload and framing."""
    return len(json.dumps(chat, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 16


def messages(prompt, system=None):
    result = []
    if system:
        result.append({"role": "system", "content": system})
    result.append({"role": "user", "content": prompt})
    return result


def configured_counter():
    value = os.environ.get("MNEMOTRACE_TOKEN_COUNTER")
    if not value:
        return None
    module, separator, name = value.partition(":")
    if not separator or not module or not name:
        raise TokenCounterConfigurationError("MNEMOTRACE_TOKEN_COUNTER must be module:function")
    try:
        counter = getattr(import_module(module), name, None)
    except ImportError as exc:
        raise TokenCounterConfigurationError("Cannot import MNEMOTRACE_TOKEN_COUNTER") from exc
    if not callable(counter):
        raise TokenCounterConfigurationError("Configured token counter must be callable")
    return counter


def require_counter(llm):
    check = getattr(llm, "require_token_counter", None)
    if check is not None:
        check()
    elif not callable(getattr(llm, "count_tokens", None)):
        raise TokenCounterConfigurationError("Configure MNEMOTRACE_TOKEN_COUNTER for this extraction model")


def count_tokens(llm, prompt, system=None):
    require_counter(llm)
    count = llm.count_tokens(messages(prompt, system))
    if type(count) is not int or count < 0:
        raise TokenCounterConfigurationError("Token counter must return a nonnegative integer")
    return count


def input_limit(llm, config, output_tokens):
    capacity = getattr(llm, "context_window", None)
    if capacity is None:
        return config.memory_max_input_tokens
    if type(capacity) is not int or capacity <= 0:
        raise ModelContextConfigurationError("Model context capacity must be a positive integer")
    return min(config.memory_max_input_tokens, capacity - output_tokens)
