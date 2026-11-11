"""习惯画像复用原语言展示句，同时保持规范事实和已有回退。"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine


@pytest.mark.parametrize("sentence", [
    "每周阅读示例书",
    "Reads an example book every week.",
])
def test_llm_display_reaches_memory_profile_without_another_model_call(sentence):
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=""):
            self.calls += 1
            return json.dumps([{
                "subject": "user", "predicate": "weekly", "object": "reads example-book",
                "text": sentence,
            }], ensure_ascii=False)

    llm = FakeLLM()
    memory = MemoryEngine(llm=llm)
    episode = memory.add(sentence, user_id="synthetic-profile-user")
    assert memory.consolidate()["facts_added"] == 1
    fact, = memory.fact_store.values()
    assert fact.display == sentence
    assert fact.text == sentence
    assert fact.provenance == [episode.id]
    before = asdict(fact)
    assert llm.calls == 1

    assert memory.profile("synthetic-profile-user")["needs_refresh"]
    assert asdict(memory.fact_store.get(fact.id)) == before
    assert llm.calls == 1
