"""习惯画像复用原语言展示句，同时保持规范事实和已有回退。"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine
from mnemotrace.consolidate.structured import build_structured_profile
from mnemotrace.types import Fact


@pytest.mark.parametrize(("predicate", "obj", "display"), [
    ("weekly", "reads example-book", "每周阅读示例书"),
    ("weekly", "reads example-book", "Reads an example book every week."),
    ("weekly", "阅读示例书", "Reads an example book every week."),
    ("weekly_catalogs", "图书", "每周整理图书"),
])
def test_habit_uses_source_language_display_without_changing_fact(predicate, obj, display):
    fact = Fact("user", predicate, obj, display=display,
                provenance=["synthetic-episode"], supersedes="synthetic-previous-fact")
    before = asdict(fact)

    profile = build_structured_profile([fact], "user")

    assert profile["habits"] == [{
        "text": display,
        "evidence": {"kind": "mentions", "source_count": 1, "reinforcement_count": 0},
        "fact_id": fact.id,
    }]
    assert profile["counts"]["habits"] == 1
    assert asdict(fact) == before


@pytest.mark.parametrize("display", ["", " \t\n"])
@pytest.mark.parametrize(("predicate", "obj", "text", "expected"), [
    ("weekly", "reads example-book", "I read an example book every week.",
     "I read an example book every week."),
    ("habit", "阅读", "", "习惯 阅读"),
    ("weekly", "阅读", "", "weekly 阅读"),
    ("weekly_catalogs", "图书", "", "weekly catalogs 图书"),
])
def test_habit_without_display_keeps_existing_fallback(predicate, obj, text, expected, display):
    fact = Fact("user", predicate, obj, text=text, display=display)
    before = asdict(fact)

    habit = build_structured_profile([fact], "user")["habits"][0]

    assert habit["text"] == expected
    assert habit["fact_id"] == fact.id
    assert asdict(fact) == before


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
    assert fact.text == "synthetic-profile-user weekly reads example-book"
    assert fact.provenance == [episode.id]
    before = asdict(fact)
    assert llm.calls == 1

    profile = memory.structured_profile("synthetic-profile-user")

    assert profile["habits"] == [{
        "text": sentence,
        "evidence": {"kind": "mentions", "source_count": 1, "reinforcement_count": 0},
        "fact_id": fact.id,
    }]
    assert asdict(memory.fact_store.get(fact.id)) == before
    assert llm.calls == 1
