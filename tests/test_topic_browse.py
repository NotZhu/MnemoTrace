"""存储主题驱动画像与浏览；不在读取时重新分类。"""
from dataclasses import asdict

import pytest

from mnemotrace import Memory
from mnemotrace.service import MemoryService
from mnemotrace.types import Fact


@pytest.mark.parametrize(("topic", "group"), [
    ("food", "饮食"), ("travel", "出行"), ("music", "音乐"), ("film_tv", "影视"),
    ("sports", "运动"), ("reading", "阅读"), ("hobbies", "兴趣"), ("environment", "环境"),
    ("health", "健康"), ("work", "工作"), ("learning", "学习"), ("technology", "技术"),
    ("finance", "财务"), ("relationships", "人际"),
])
def test_profile_consumes_stored_topic_even_when_relation_and_object_disagree(topic, group):
    mem = Memory()
    fact = mem.add_fact("user", "favorite_music", "茶", topic=topic)
    before = asdict(fact)
    item, = mem.structured_profile()["preferences"][group]
    assert (item["topic"], item["topic_source"], item["category"]) == (topic, "user", group)
    assert fact.category == "偏好" and asdict(fact) == before


@pytest.mark.parametrize("source", ["unset", "user"])
def test_unknown_profile_never_reinfers_topic(source):
    mem = Memory()
    fact = Fact("user", "favorite_music", "茶", category="偏好", topic_source=source)
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    before = asdict(fact)
    item, = mem.structured_profile()["preferences"]["其他"]
    assert (item["topic"], item["topic_source"]) == ("unknown", source)
    assert asdict(fact) == before


def test_profile_keeps_restrictions_separate_and_groups_in_topic_order():
    mem = Memory()
    mem.add_fact("user", "prefers", "合成未知偏好", topic="unknown")
    mem.add_fact("user", "prefers", "合成健康科普", topic="health")
    restriction = mem.add_fact("user", "allergic_to", "合成禁忌", topic="unknown")
    mem.add_fact("user", "prefers", "合成音乐", topic="music")
    profile = mem.structured_profile()
    assert list(profile["preferences"]) == ["音乐", "健康禁忌", "健康", "其他"]
    assert profile["preferences"]["健康禁忌"][0]["topic"] == "unknown"
    assert profile["preferences"]["健康"][0]["polarity"] == "like"
    assert restriction.topic_source == "user"


def test_topic_filter_intersects_before_paging_without_changing_facets_or_episodes(tmp_path):
    svc = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    mem = svc.get("u")
    mem.add("Synthetic raw episode about 茶", user_id="u")
    first = svc.add_fact("u", "user", "prefers", "茶一", topic="food", category="合成分类甲")
    svc.add_fact("u", "user", "prefers", "茶二", topic="music", category="合成分类乙")
    svc.add_fact("u", "user", "prefers", "茶三", topic="food", category="合成分类甲")
    svc.add_fact("u", "user", "lesson", "合成结论", topic="food")
    svc.add_fact("u", "user", "prefers", "私密合成事实", topic="finance", sensitive=True)
    svc.add_fact("v", "user", "prefers", "其他空间", topic="technology")
    page = svc.memories("u", topic="food", kind="attributes", status="live", q="茶",
                        category="合成分类甲", facts_limit=1, include_sensitive=True)
    assert page["facts_page"]["total"] == 2 and len(page["facts"]) == 1
    assert set(page["fact_categories"]) == {"合成分类甲", "合成分类乙", "偏好"}
    assert len(page["episodes"]) == 1
    nxt = svc.memories("u", topic="food", kind="attributes", status="live", q="茶",
                       category="合成分类甲", facts_limit=1, facts_offset=1, include_sensitive=True,
                       expected_revision=page["data_revision"], evaluated_at=page["evaluated_at"])
    assert {page["facts"][0]["id"], nxt["facts"][0]["id"]} >= {first["id"]}
    assert not nxt["facts_page"]["has_more"]
    assert svc.memories("u", topic="finance")["facts"] == []
    assert svc.memories("u", topic="technology")["facts"] == []
    assert len(svc.memories("u", topic="food", kind="outcomes")["facts"]) == 1


@pytest.mark.parametrize("topic", ["", "Music", "音乐", [], 1])
def test_invalid_topic_filter_is_rejected(tmp_path, topic):
    svc = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    with pytest.raises(ValueError, match="topic"):
        svc.memories("u", topic=topic)


def test_unknown_filter_matches_both_origins_and_hot_cold_facts(tmp_path):
    svc = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    mem = svc.get("u")
    manual = mem.add_fact("user", "prefers", "茶", user_id="u", topic="unknown")
    old = Fact("user", "prefers", "咖啡", user_id="u", category="偏好")
    old.embedding = mem.embedder.embed(old.text)
    mem.cold_store.upsert(old.id, old.embedding, old)
    mem.add_fact("user", "prefers", "羽毛球", user_id="u")
    result = svc.memories("u", topic="unknown")
    assert {f["id"] for f in result["facts"]} == {manual.id, old.id}
    assert {f["topic_source"] for f in result["facts"]} == {"user", "unset"}
