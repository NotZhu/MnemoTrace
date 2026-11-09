"""自动主题的优先级、写入边界和模型调用成本；全部使用合成数据。"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from mnemotrace import Config

from tests.model_fixtures import MemoryEngine
from mnemotrace.consolidate.outcomes import OUTCOME_CATEGORY
from mnemotrace.types import Fact


class RecordingLLM:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def complete(self, prompt, system="", **kwargs):
        self.calls.append((prompt, system, kwargs))
        return json.dumps(self.rows, ensure_ascii=False)


def extracted(row, config=None):
    model = RecordingLLM([{"subject": "user", "predicate": "prefers", **row}])
    mem = MemoryEngine(llm=model, config=config)
    episode = mem.add("Synthetic conversation", user_id="synthetic-user")
    assert mem.consolidate()["facts_added"] == 1
    fact, = mem.fact_store.values()
    assert fact.provenance == [episode.id] and len(model.calls) == 1
    return fact, model


@pytest.mark.parametrize(("row", "expected"), [
    ({"object": "Inception", "topic": "film_tv"}, ("film_tv", "llm")),
    ({"object": "茶", "topic": "unknown"}, ("unknown", "unset")),
    ({"object": "茶"}, ("unknown", "unset")),
    ({"object": "茶", "topic": "音乐"}, ("unknown", "unset")),
    ({"object": "茶", "topic": "Music"}, ("unknown", "unset")),
    ({"object": "茶", "topic": ""}, ("unknown", "unset")),
    ({"object": "茶", "topic": None}, ("unknown", "unset")),
    ({"object": "茶", "topic": []}, ("unknown", "unset")),
    ({"object": "茶", "topic": {}}, ("unknown", "unset")),
    ({"object": "茶", "topic": True}, ("unknown", "unset")),
    ({"predicate": "favorite_music", "object": "茶", "topic": "food"}, ("food", "llm")),
    ({"predicate": "favorite_music", "object": "茶", "topic": "unknown"}, ("unknown", "unset")),
])
def test_llm_topics_preserve_facts_and_respect_precedence(row, expected):
    fact, _ = extracted(row)
    assert (fact.topic, fact.topic_source) == expected


def test_model_labels_are_independent_of_server_owned_origin():
    fact, _ = extracted({"object": "synthetic-value", "topic": "health", "topic_source": "user",
                          "source": "user", "category": "健康", "sensitive": True})
    assert (fact.topic, fact.topic_source) == ("health", "llm")
    assert fact.source == "extracted" and fact.category == "健康" and fact.sensitive is True


@pytest.mark.parametrize("hint", [None, "unknown", "music"])
def test_topics_follow_final_normalized_objects_without_losing_abstention(hint):
    row = {"predicate": "likes", "object": "the idea of 茶"}
    if hint is not None:
        row["topic"] = hint
    fact, _ = extracted(row)
    assert fact.object == "茶"
    assert (fact.topic, fact.topic_source) == {
        None: ("unknown", "unset"), "unknown": ("unknown", "unset"), "music": ("music", "llm"),
    }[hint]
    fact, _ = extracted({"predicate": "likes", "object": "the idea of 茶"},
                         Config(preference_object_normalization=False))
    assert fact.object == "the idea of 茶" and fact.topic == "unknown"


def test_manual_updates_do_not_call_models():
    model = RecordingLLM([])
    mem = MemoryEngine(llm=model)
    fact = mem.add_fact("user", "prefers", "茶", topic="unknown")
    mem.update_fact(fact.id, reset_topic=True)
    assert (fact.topic, fact.topic_source) == ("unknown", "unset")
    mem.update_fact(fact.id, object="羽毛球")
    assert (fact.topic, fact.topic_source) == ("unknown", "unset")
    mem.update_fact(fact.id, object="看书", topic="music")
    assert (fact.topic, fact.topic_source) == ("music", "user")
    assert model.calls == []


def test_reset_is_metadata_only_even_on_historical_facts():
    mem = MemoryEngine()
    fact = Fact("user", "prefers", "茶", source="extracted", invalid_at=2, expired_at=3,
                topic="music", topic_source="user", category="偏好")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    before = asdict(fact)
    mem.update_fact(fact.id, reset_topic=True)
    assert asdict(fact) == {**before, "topic": "unknown", "topic_source": "unset"}


def test_invalid_topics_are_reported_without_echoing_payloads(caplog):
    rows = [{"subject": "user", "predicate": "prefers", "object": obj, "topic": "SYNTHETIC_SECRET"}
            for obj in ["茶", "羽毛球", "看书"]]
    mem = MemoryEngine(llm=RecordingLLM(rows))
    mem.add("SYNTHETIC_CONVERSATION")
    assert mem.consolidate()["facts_added"] == 3
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "3" in warnings[0].getMessage()
    assert "SYNTHETIC_SECRET" not in caplog.text and "SYNTHETIC_CONVERSATION" not in caplog.text


def test_snapshot_import_does_not_backfill_topics():
    mem = MemoryEngine()
    fact = Fact("user", "prefers", "茶", category="偏好")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    model = RecordingLLM([])
    restored = MemoryEngine(llm=model)
    restored.import_snapshot({"facts": [asdict(fact)], "episodes": []})
    assert restored.fact_store.values()[0].topic_source == "unset" and model.calls == []


def test_manual_unknown_survives_duplicate_extraction():
    mem = MemoryEngine()
    fact = mem.add_fact("default", "likes", "tea", topic="unknown", valid_at=100)
    mem.add("I like tea.", event_time=100)
    mem.consolidate()
    assert len(mem.fact_store.values()) == 1
    assert (fact.topic, fact.topic_source) == ("unknown", "user")


def test_parallel_extraction_preserves_independent_hints():
    class ParallelLLM:
        def __init__(self):
            self.calls = []

        def complete(self, prompt, system="", **kwargs):
            self.calls.append(prompt)
            index = next(i for i in range(3) if f"Synthetic row {i}" in prompt)
            obj, topic = [("茶", "food"), ("羽毛球", "sports"), ("Inception", "film_tv")][index]
            return json.dumps([{"subject": "user", "predicate": "prefers", "object": obj, "topic": topic}])
    model = ParallelLLM()
    mem = MemoryEngine(llm=model)
    for index in range(3):
        mem.add(f"Synthetic row {index}", event_time=index + 1)
    assert mem.consolidate()["facts_added"] == 3
    assert {(f.object, f.topic, f.topic_source) for f in mem.fact_store.values()} == {
        ("茶", "food", "llm"), ("羽毛球", "sports", "llm"), ("Inception", "film_tv", "llm"),
    }
    assert len(model.calls) == 3


def test_outcome_edits_recompute_using_statement_and_reason():
    mem = MemoryEngine()
    fact = Fact("Health Bank Session", "lesson", "采用一项已验证的合成方案来检查实际效果",
                text="采用一项已验证的合成方案来检查实际效果 （依据：数据库需要索引）",
                category=OUTCOME_CATEGORY, topic="music", topic_source="user")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    mem.update_fact(fact.id, statement="采用另一项已验证的合成方案来检查实际效果")
    assert (fact.topic, fact.topic_source, fact.category) == ("unknown", "unset", OUTCOME_CATEGORY)


def test_outcome_reset_ignores_session_even_with_default_rendered_text():
    mem = MemoryEngine()
    fact = mem.add_fact("数据库索引合成会话", "lesson", "采用一项已经验证的合成实现方案", topic="music")
    mem.update_fact(fact.id, reset_topic=True)
    assert (fact.topic, fact.topic_source) == ("unknown", "unset")


@pytest.mark.parametrize("obj", ["teammate", "engineer", "bookkeeping", "流行", "资料片段"])
def test_automatic_topics_reject_similar_fragments(obj):
    fact = MemoryEngine().add_fact("user", "uses", obj)
    assert (fact.topic, fact.topic_source) == ("unknown", "unset")
