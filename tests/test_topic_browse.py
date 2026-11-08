"""存储主题驱动画像与浏览；不在读取时重新分类。"""
from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine
from mnemotrace.types import Fact


@pytest.mark.parametrize(("topic", "group"), [
    ("food", "饮食"), ("travel", "出行"), ("music", "音乐"), ("film_tv", "影视"),
    ("sports", "运动"), ("reading", "阅读"), ("hobbies", "兴趣"), ("environment", "环境"),
    ("health", "健康"), ("work", "工作"), ("learning", "学习"), ("technology", "技术"),
    ("finance", "财务"), ("relationships", "人际"),
])
def test_profile_consumes_stored_topic_even_when_relation_and_object_disagree(topic, group):
    mem = MemoryEngine()
    fact = mem.add_fact("user", "favorite_music", "茶", topic=topic, category="偏好")
    before = asdict(fact)
    item, = mem.structured_profile()["preferences"][group]
    assert (item["topic"], item["topic_source"], item["category"]) == (topic, "user", group)
    assert fact.category == "偏好" and asdict(fact) == before


@pytest.mark.parametrize("source", ["unset", "user"])
def test_unknown_profile_never_reinfers_topic(source):
    mem = MemoryEngine()
    fact = Fact("user", "favorite_music", "茶", category="偏好", topic_source=source)
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    before = asdict(fact)
    item, = mem.structured_profile()["preferences"]["其他"]
    assert (item["topic"], item["topic_source"]) == ("unknown", source)
    assert asdict(fact) == before


def test_profile_keeps_restrictions_separate_and_groups_in_topic_order():
    mem = MemoryEngine()
    mem.add_fact("user", "prefers", "合成未知偏好", topic="unknown")
    mem.add_fact("user", "prefers", "合成健康科普", topic="health")
    restriction = mem.add_fact("user", "allergic_to", "合成禁忌", topic="unknown")
    mem.add_fact("user", "prefers", "合成音乐", topic="music")
    profile = mem.structured_profile()
    assert list(profile["preferences"]) == ["音乐", "健康禁忌", "健康", "其他"]
    assert profile["preferences"]["健康禁忌"][0]["topic"] == "unknown"
    assert profile["preferences"]["健康"][0]["polarity"] == "like"
    assert restriction.topic_source == "user"
