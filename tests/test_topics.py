"""单一事实主题的人工管理、历史保护和严格持久化契约。"""
from __future__ import annotations

import copy
from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine
from mnemotrace.types import Fact


def test_profile_follows_manual_topic_without_changing_fact_classification():
    mem = MemoryEngine()
    mem.add("I like tea.")
    mem.consolidate()
    fact, = mem.fact_store.values()
    assert (fact.topic, fact.topic_source, fact.category) == ("food", "rule", "偏好")
    mem.update_fact(fact.id, topic="music")
    item, = mem.structured_profile()["preferences"]["音乐"]
    assert item["fact_id"] == fact.id


@pytest.mark.parametrize("topic", ["food", "travel", "music", "film_tv", "sports", "reading",
                                    "hobbies", "environment", "health", "work", "learning",
                                    "technology", "finance", "relationships", "unknown"])
def test_manual_topic_values_and_reset_are_distinct(topic):
    mem = MemoryEngine()
    fact = mem.add_fact("user", "prefers", "茶", topic=topic)
    assert (fact.topic, fact.topic_source) == (topic, "user")
    assert fact.category == "偏好" and fact.sensitive is False
    mem.update_fact(fact.id, topic=None)
    assert (fact.topic, fact.topic_source) == (topic, "user")
    mem.update_fact(fact.id, reset_topic=True)
    assert (fact.topic, fact.topic_source) == ("food", "rule")


@pytest.mark.parametrize("resubmit", [False, True])
def test_topic_edit_preserves_extracted_history_and_graph(resubmit):
    mem = MemoryEngine()
    mem.add("I live in Beijing.", event_time=1)
    mem.consolidate()
    old, = mem.fact_store.values()
    mem.add_fact(old.subject, old.predicate, "Shanghai", valid_at=2)
    assert old.source == "extracted" and not old.is_live()
    before = asdict(old)
    graph = mem.graph_data()
    patch = {key: getattr(old, key) for key in ("subject", "predicate", "object")} if resubmit else {}
    mem.update_fact(old.id, topic="travel", **patch)
    assert asdict(old) == {**before, "topic": "travel", "topic_source": "user"}
    assert mem.graph_data() == graph
    mem.update_fact(old.id, reset_topic=True)
    assert asdict(old) == {**before, "topic": "unknown", "topic_source": "unset"}


@pytest.mark.parametrize("patch", [{"object": "coffee"}, {"predicate": "likes"}, {"subject": "Synthetic"}])
def test_content_edit_resets_old_topic_unless_explicitly_replaced(patch):
    mem = MemoryEngine()
    fact = mem.add_fact("user", "prefers", "tea", topic="food")
    mem.update_fact(fact.id, **patch)
    assert (fact.topic, fact.topic_source) == ("food", "rule")
    mem.update_fact(fact.id, object="jazz", topic="music")
    assert (fact.topic, fact.topic_source) == ("music", "user")


@pytest.mark.parametrize("topic", ["", "音乐", "Music", " food ", "other", [], {}, 1, True])
def test_invalid_topic_fails_before_any_memory_mutation(topic):
    mem = MemoryEngine()
    fact = mem.add_fact("user", "prefers", "tea", topic="food")
    before = asdict(fact)
    graph = mem.graph_data()
    with pytest.raises(ValueError, match="topic"):
        mem.update_fact(fact.id, object="coffee", topic=topic)
    assert asdict(fact) == before and mem.graph_data() == graph
    with pytest.raises(ValueError, match="topic"):
        mem.add_fact("user", "prefers", "coffee", topic=topic)
    assert len(mem.fact_store.values()) == 1


def test_topic_and_reset_conflict_does_not_edit_content():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "prefers", "tea")
    before = asdict(fact)
    with pytest.raises(ValueError, match="topic"):
        mem.update_fact(fact.id, object="coffee", topic="food", reset_topic=True)
    assert asdict(fact) == before


@pytest.mark.parametrize(("topic", "source"), [
    ("unknown", "unset"), ("unknown", "user"), ("music", "user"),
    ("food", "rule"), ("technology", "llm"),
])
@pytest.mark.parametrize("tier", ["hot", "cold"])
def test_topics_roundtrip_exported_records(topic, source, tier):
    mem = MemoryEngine()
    fact = Fact("Synthetic", "prefers", "synthetic-value", user_id="u", topic=topic, topic_source=source,
                category="偏好")
    fact.embedding = mem.embedder.embed(fact.text)
    store = mem.fact_store if tier == "hot" else mem.cold_store
    store.upsert(fact.id, fact.embedding, fact)
    snapshot = {"facts": [asdict(record) for record in mem._all_facts()], "episodes": []}
    dst = MemoryEngine()
    dst.import_snapshot(snapshot)
    imported, = dst.fact_store.values()
    assert (imported.topic, imported.topic_source) == (topic, source)
    dst.update_fact(imported.id, topic="reading")
    dst.import_snapshot(snapshot)
    assert (imported.topic, imported.topic_source) == ("reading", "user")


@pytest.mark.parametrize(("topic", "source"), [
    ("music", "unset"), ("unknown", "rule"), ("unknown", "llm"),
    ("music", "manual"), ("invalid", "user"),
])
def test_invalid_topic_pairs_cannot_be_constructed(topic, source):
    with pytest.raises(ValueError, match="topic"):
        Fact("user", "prefers", "tea", topic=topic, topic_source=source)


def test_invalid_snapshot_topic_rejected_before_partial_import():
    fact = Fact("user", "prefers", "tea", topic="food", topic_source="user")
    snapshot = {"facts": [asdict(fact)], "episodes": []}
    invalid = copy.deepcopy(snapshot["facts"][0])
    invalid.update(id="synthetic-invalid", topic="invalid")
    snapshot["facts"].append(invalid)
    dst = MemoryEngine()
    with pytest.raises(ValueError, match="topic"):
        dst.import_snapshot(snapshot)
    assert dst.fact_store.values() == [] and dst.episodes_doc.values() == []


def test_manual_topic_writes_do_not_call_llm():
    class NoCalls:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            return "[]"
    model = NoCalls()
    mem = MemoryEngine(llm=model)
    fact = mem.add_fact("user", "prefers", "tea", topic="food")
    mem.update_fact(fact.id, topic="reading")
    mem.update_fact(fact.id, reset_topic=True)
    assert (fact.topic, fact.topic_source) == ("food", "rule")
    assert model.calls == 0


def test_outcome_content_edit_resets_topic_and_preserves_outcome_category():
    from mnemotrace.consolidate.outcomes import OUTCOME_CATEGORY
    mem = MemoryEngine()
    fact = Fact("synthetic-session", "lesson", "Use the synthetic option",
                text="Use the synthetic option （依据：synthetic reason）", category=OUTCOME_CATEGORY,
                topic="technology", topic_source="user")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    mem.update_fact(fact.id, statement="Use the revised synthetic option")
    assert (fact.topic, fact.topic_source, fact.category) == ("unknown", "unset", OUTCOME_CATEGORY)
    assert "synthetic reason" in fact.text
