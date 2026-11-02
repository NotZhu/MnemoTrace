"""自动主题的优先级、写入边界和模型调用成本；全部使用合成数据。"""
from __future__ import annotations
from tests.model_fixtures import outcome_facts

import json
from dataclasses import asdict

import pytest

from mnemotrace import Config

from tests.model_fixtures import Memory
from mnemotrace.consolidate.outcomes import OUTCOME_CATEGORY
from tests.model_fixtures import MemoryService
from mnemotrace.types import Episode, Fact


class RecordingLLM:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def complete(self, prompt, system="", **kwargs):
        self.calls.append((prompt, system, kwargs))
        return json.dumps(self.rows, ensure_ascii=False)


def extracted(row, config=None):
    model = RecordingLLM([{"subject": "user", "predicate": "prefers", **row}])
    mem = Memory(llm=model, config=config)
    episode = mem.add("Synthetic conversation", user_id="synthetic-user")
    assert mem.consolidate()["facts_added"] == 1
    fact, = mem.fact_store.values()
    assert fact.provenance == [episode.id] and len(model.calls) == 1
    return fact, model


@pytest.mark.parametrize(("predicate", "obj", "expected"), [
    ("prefers", "茶", "food"), ("prefers", "晚餐", "food"), ("prefers", "早饭", "food"),
    ("prefers", "羽毛球", "sports"), ("prefers", "看书", "reading"), ("prefers", "短片", "film_tv"),
    ("uses", "storage engine", "technology"), ("prefers", "流行的编程框架", "technology"),
    ("prefers", "整理参考资料", "unknown"), ("prefers", "保留原始对话片段", "unknown"),
    ("prefers", "team collaboration", "unknown"), ("prefers", "秘书", "unknown"),
    ("prefers", "有游泳池的酒店", "travel"), ("prefers", "数据库与晚餐", "unknown"),
    ("prefers", "有晚餐和游泳池的酒店", "unknown"),
    ("works_at", "Storage Labs", "work"), ("employer", "SyntheticCo", "work"),
    ("occupation", "synthetic-role", "work"), ("job", "synthetic-role", "work"),
    ("profession", "synthetic-role", "work"), ("studied_at", "Synthetic School", "learning"),
    ("graduated_from", "Synthetic School", "learning"), ("education", "synthetic-degree", "learning"),
    ("degree", "synthetic-degree", "learning"), ("has_disease", "synthetic-value", "health"),
    ("allergic_to", "花生", "health"), ("cannot_eat", "花生", "health"),
    ("salary", "24", "finance"), ("bank_account", "synthetic-value", "finance"),
    ("married_to", "Synthetic Name", "relationships"), ("friends_with", "Synthetic Name", "relationships"),
    ("favorite_music", "茶", "music"), ("FAVOURITE--MOVIES", "Synthetic Title", "film_tv"),
    ("favorite_book", "tea history", "reading"), ("favorite_drink", "synthetic-value", "food"),
    ("favorite_sport", "synthetic-value", "sports"), ("favorite_hotel", "synthetic-value", "travel"),
    ("prefers_quiet_environments", "synthetic-value", "environment"),
    ("age", "24", "unknown"), ("name", "Synthetic Name", "unknown"),
    ("lives_in", "北京", "unknown"), ("uses_bank_api", "synthetic-value", "unknown"),
    ("prefers", "Python 运行环境", "technology"), ("prefers", "股票", "finance"),
])
def test_rule_topics_use_relations_or_unambiguous_object_evidence(predicate, obj, expected):
    mem = Memory()
    fact = mem.add_fact("Age Health Bank Lab", predicate, obj)
    assert (fact.topic, fact.topic_source) == (expected, "unset" if expected == "unknown" else "rule")
    if obj == "storage engine":
        assert fact.category == "其他" and fact.sensitive is False


@pytest.mark.parametrize("obj", ["黑茶", "普洱", "白茶", "龙井", "披萨", "寿司"])
def test_food_topic_additions_drive_profile_grouping_and_preserve_conflict_abstention(obj):
    mem = Memory()
    fact = mem.add_fact("user", "prefers", obj)
    assert (fact.topic, fact.topic_source) == ("food", "rule")
    item, = mem.structured_profile()["preferences"]["饮食"]
    assert item["fact_id"] == fact.id
    conflict = Memory().add_fact("user", "prefers", f"{obj}与数据库")
    assert (conflict.topic, conflict.topic_source) == ("unknown", "unset")


@pytest.mark.parametrize(("row", "expected"), [
    ({"object": "Inception", "topic": "film_tv"}, ("film_tv", "llm")),
    ({"object": "茶", "topic": "unknown"}, ("unknown", "unset")),
    ({"object": "茶"}, ("food", "rule")),
    ({"object": "茶", "topic": "音乐"}, ("food", "rule")),
    ({"object": "茶", "topic": "Music"}, ("food", "rule")),
    ({"object": "茶", "topic": ""}, ("food", "rule")),
    ({"object": "茶", "topic": None}, ("food", "rule")),
    ({"object": "茶", "topic": []}, ("food", "rule")),
    ({"object": "茶", "topic": {}}, ("food", "rule")),
    ({"object": "茶", "topic": True}, ("food", "rule")),
    ({"predicate": "favorite_music", "object": "茶", "topic": "food"}, ("music", "rule")),
    ({"predicate": "favorite_music", "object": "茶", "topic": "unknown"}, ("music", "rule")),
])
def test_llm_topics_preserve_facts_and_respect_precedence(row, expected):
    fact, _ = extracted(row)
    assert (fact.topic, fact.topic_source) == expected


def test_model_cannot_claim_manual_origin_or_override_classification():
    fact, _ = extracted({"object": "synthetic-value", "topic": "health", "topic_source": "user",
                          "source": "user", "category": "forged", "sensitive": True})
    assert (fact.topic, fact.topic_source) == ("health", "llm")
    assert fact.source == "extracted" and fact.category == "偏好" and fact.sensitive is False


@pytest.mark.parametrize(("enabled", "expected"), [(True, ("music", "llm")), (False, ("food", "rule"))])
def test_config_controls_llm_topics_without_another_call(enabled, expected):
    fact, model = extracted({"object": "茶", "topic": "music"}, Config(llm_topics=enabled))
    assert (fact.topic, fact.topic_source) == expected
    assert ("附加主题协议" in model.calls[0][1]) is enabled
    assert model.calls[0][2] == {}




@pytest.mark.parametrize("hint", [None, "unknown", "music"])
def test_topics_follow_final_normalized_objects_without_losing_abstention(hint):
    row = {"predicate": "likes", "object": "the idea of 茶"}
    if hint is not None:
        row["topic"] = hint
    fact, _ = extracted(row)
    assert fact.object == "茶"
    assert (fact.topic, fact.topic_source) == {
        None: ("food", "rule"), "unknown": ("unknown", "unset"), "music": ("music", "llm"),
    }[hint]
    fact, _ = extracted({"predicate": "likes", "object": "the idea of 茶"},
                         Config(preference_object_normalization=False))
    assert fact.object == "the idea of 茶" and fact.topic == "unknown"


def test_manual_updates_do_not_call_models():
    model = RecordingLLM([])
    mem = Memory(llm=model)
    fact = mem.add_fact("user", "prefers", "茶", topic="unknown")
    mem.update_fact(fact.id, reset_topic=True)
    assert (fact.topic, fact.topic_source) == ("food", "rule")
    mem.update_fact(fact.id, object="羽毛球")
    assert (fact.topic, fact.topic_source) == ("sports", "rule")
    mem.update_fact(fact.id, object="看书", topic="music")
    assert (fact.topic, fact.topic_source) == ("music", "user")
    assert model.calls == []


def test_reset_is_metadata_only_even_on_historical_facts():
    mem = Memory()
    fact = Fact("user", "prefers", "茶", source="extracted", invalid_at=2, expired_at=3,
                topic="music", topic_source="user", category="偏好")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    before = asdict(fact)
    mem.update_fact(fact.id, reset_topic=True)
    assert asdict(fact) == {**before, "topic": "food", "topic_source": "rule"}


@pytest.mark.parametrize("hint", ["technology", "unknown", None])
def test_outcomes_share_topic_protocol_and_keep_one_call(hint):
    row = {"kind": "lesson", "statement": "使用明确的实现方案来验证合成案例", "why": "数据库需要索引"}
    if hint is not None:
        row["topic"] = hint
    model = RecordingLLM([row])
    facts = outcome_facts(model, [Episode("Synthetic discussion")], "u", session_id="Bank Health Lab")
    fact, = facts
    assert fact.category == OUTCOME_CATEGORY and len(model.calls) == 1
    assert (fact.topic, fact.topic_source) == {
        "technology": ("technology", "llm"), "unknown": ("unknown", "unset"), None: ("technology", "rule"),
    }[hint]
    assert "附加主题协议" in model.calls[0][1]


def test_invalid_topics_are_reported_without_echoing_payloads(caplog):
    rows = [{"subject": "user", "predicate": "prefers", "object": obj, "topic": "SYNTHETIC_SECRET"}
            for obj in ["茶", "羽毛球", "看书"]]
    mem = Memory(llm=RecordingLLM(rows))
    mem.add("SYNTHETIC_CONVERSATION")
    assert mem.consolidate()["facts_added"] == 3
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "3" in warnings[0].getMessage()
    assert "SYNTHETIC_SECRET" not in caplog.text and "SYNTHETIC_CONVERSATION" not in caplog.text


def test_load_and_snapshot_import_do_not_backfill_topics(tmp_path):
    mem = Memory()
    fact = Fact("user", "prefers", "茶", category="偏好")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    mem.save(str(tmp_path / "store"))
    model = RecordingLLM([])
    restored = Memory.open(str(tmp_path / "store"), llm=model)
    assert restored.fact_store.get(fact.id).topic_source == "unset" and model.calls == []
    svc = MemoryService(data_dir=str(tmp_path / "service"), embedder_name="hashing", llm_name="")
    svc.import_("u", data={"facts": [asdict(fact)], "episodes": []})
    assert svc.memories("u")["facts"][0]["topic_source"] == "unset"


def test_manual_unknown_survives_duplicate_extraction():
    mem = Memory()
    fact = mem.add_fact("default", "likes", "tea", topic="unknown")
    mem.add("I like tea.")
    mem.consolidate()
    assert len(mem.fact_store.values()) == 1
    assert (fact.topic, fact.topic_source) == ("unknown", "user")


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("model_topic", ["music", "SYNTHETIC_INVALID_TOPIC"])
def test_service_switch_applies_to_attributes_and_outcomes(tmp_path, monkeypatch, caplog, enabled, model_topic):
    monkeypatch.setenv("MNEMOTRACE_LLM_TOPICS", "1" if enabled else "0")
    monkeypatch.delenv("MNEMOTRACE_SESSION_OUTCOMES", raising=False)

    class LifecycleLLM:
        def __init__(self):
            self.calls = []

        def complete(self, prompt, system="", **kwargs):
            self.calls.append((prompt, system, kwargs))
            if "JSON 事实" in prompt:
                return json.dumps([{"subject": "user", "predicate": "prefers", "object": "茶",
                                    "topic": model_topic}])
            if prompt.endswith("摘要："):
                return "Synthetic summary"
            return json.dumps([{"kind": "decision", "statement": "决定采用一项已经验证的合成实现方案",
                                "why": "数据库需要索引", "topic": model_topic}])

    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    model = LifecycleLLM()
    service.llm = model
    service.remember("u", "Synthetic conversation", session_id="synthetic-session", scope="long")
    service.close_session("u", "synthetic-session", summarize=False)
    facts = service.get("u").fact_store.values()
    attr, = [f for f in facts if f.predicate == "prefers"]
    outcome, = [f for f in facts if f.predicate == "decision"]
    accepted = enabled and model_topic == "music"
    assert (attr.topic, attr.topic_source) == (("music", "llm") if accepted else ("food", "rule"))
    assert (outcome.topic, outcome.topic_source) == (("music", "llm") if accepted else ("technology", "rule"))
    attributes = [system for prompt, system, _ in model.calls if "JSON 事实" in prompt]
    outcomes = [system for prompt, system, _ in model.calls if "<session>" in prompt]
    summaries = [system for prompt, system, _ in model.calls if prompt.endswith("摘要：")]
    assert len(attributes) == len(outcomes) == 1
    assert summaries == []
    assert len(model.calls) == 2  # Short events do not request a summary.
    assert all(("附加主题协议" in system) is enabled for system in attributes + outcomes)
    warnings = [record.getMessage() for record in caplog.records
                if record.name == "mnemotrace.consolidate.topic_rules" and record.levelname == "WARNING"]
    assert warnings == (["LLM topic fallback: invalid=1"] * 2
                        if enabled and model_topic == "SYNTHETIC_INVALID_TOPIC" else [])
    assert all(text not in caplog.text for text in (
        "SYNTHETIC_INVALID_TOPIC", "Synthetic conversation", "synthetic-session", "茶",
        "决定采用一项已经验证的合成实现方案", "数据库需要索引",
    ))


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
    mem = Memory(llm=model)
    for index in range(3):
        mem.add(f"Synthetic row {index}", event_time=index + 1)
    assert mem.consolidate()["facts_added"] == 3
    assert {(f.object, f.topic, f.topic_source) for f in mem.fact_store.values()} == {
        ("茶", "food", "llm"), ("羽毛球", "sports", "llm"), ("Inception", "film_tv", "llm"),
    }
    assert len(model.calls) == 3


def test_outcome_edits_recompute_using_statement_and_reason():
    mem = Memory()
    fact = Fact("Health Bank Session", "lesson", "采用一项已验证的合成方案来检查实际效果",
                text="采用一项已验证的合成方案来检查实际效果 （依据：数据库需要索引）",
                category=OUTCOME_CATEGORY, topic="music", topic_source="user")
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    mem.update_fact(fact.id, statement="采用另一项已验证的合成方案来检查实际效果")
    assert (fact.topic, fact.topic_source, fact.category) == ("technology", "rule", OUTCOME_CATEGORY)


def test_outcome_reset_ignores_session_even_with_default_rendered_text():
    mem = Memory()
    fact = mem.add_fact("数据库索引合成会话", "lesson", "采用一项已经验证的合成实现方案", topic="music")
    mem.update_fact(fact.id, reset_topic=True)
    assert (fact.topic, fact.topic_source) == ("unknown", "unset")


@pytest.mark.parametrize("obj", ["STORAGE ENGINE", "storage-engine", "storage_engine",
                                 "storage  engine", "storage\tengine"])
def test_automatic_topics_share_separator_boundaries(obj):
    fact = Memory().add_fact("user", "uses", obj)
    assert (fact.topic, fact.topic_source) == ("technology", "rule")


@pytest.mark.parametrize("obj", ["teammate", "engineer", "bookkeeping", "流行", "资料片段"])
def test_automatic_topics_reject_similar_fragments(obj):
    fact = Memory().add_fact("user", "uses", obj)
    assert (fact.topic, fact.topic_source) == ("unknown", "unset")


def test_extraction_failure_does_not_create_facts_or_retry_for_topics():
    class BrokenLLM:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("synthetic failure")
    model = BrokenLLM()
    from mnemotrace.extraction import ExtractionError
    with pytest.raises(ExtractionError):
        outcome_facts(model, [Episode("Synthetic discussion")], "u")
    assert model.calls == 1
