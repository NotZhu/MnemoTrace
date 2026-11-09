"""Termination and usage must survive adapters and replay without a model service."""
from types import SimpleNamespace

import pytest

from mnemotrace.llm import Completion, FakeLLM
from mnemotrace.llm.base import complete_result
from mnemotrace.llm.cache import CachedLLM
from mnemotrace.llm.litellm_llm import LiteLLMClient


def provider_client(reason, content, usage=None):
    client = object.__new__(LiteLLMClient)
    client.model, client.temperature, client.max_tokens = "synthetic", 0, 100
    client.kwargs, client.max_attempts = {}, 1
    client.retry_base, client.retry_cap = 0, 0
    client._litellm = SimpleNamespace(completion=lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=reason)],
        usage=usage))
    return client


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
def test_provider_metadata_is_retained_even_for_complete_json(reason):
    client = provider_client(reason, '{"facts":[],}', SimpleNamespace(
        prompt_tokens=23, completion_tokens=7, total_tokens=30))
    result = client.complete_result("synthetic")
    assert result.finish_reason == reason
    assert result.usage == {"prompt_tokens": 23, "completion_tokens": 7, "total_tokens": 30}
    assert client.complete("synthetic") == result.text


def test_empty_truncated_completion_is_not_retried_as_an_empty_body():
    result = provider_client("length", None).complete_result("synthetic")
    assert result.text == "" and result.finish_reason == "length" and result.usage is None


def test_plain_text_adapter_does_not_invent_metadata():
    class TextOnly:
        def complete(self, *args, **kwargs):
            return "synthetic"
    assert complete_result(TextOnly(), "p") == Completion("synthetic")


def test_cache_replays_termination_and_usage_without_model_calls(tmp_path):
    expected = Completion("{}", "length", {"total_tokens": 12})
    model = FakeLLM(responses=[expected])
    cached = CachedLLM(model, str(tmp_path))
    assert cached.complete_result("p") == expected
    other = FakeLLM()
    restored = CachedLLM(other, str(tmp_path))
    assert restored.complete_result("p") == expected
    assert len(model.calls) == 1 and not other.calls
    assert restored.stats()["hits"] == 1


def test_corrupt_completion_cache_is_recomputed(tmp_path):
    model = FakeLLM(responses=[Completion("{}", "stop")])
    cached = CachedLLM(model, str(tmp_path))
    key = cached._key("p", {"_completion_record": "text-finish-usage"})
    (tmp_path / key).write_text('{"text":false}', encoding="utf-8")
    assert cached.complete_result("p") == Completion("{}", "stop")
    assert len(model.calls) == 1


@pytest.mark.parametrize("usage", [{"total_tokens": True}, {"total_tokens": -1}, {"price": 1}])
def test_usage_rejects_invalid_counts(usage):
    with pytest.raises(ValueError):
        Completion("{}", usage=usage)
