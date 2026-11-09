"""画像按存储的主题分组，并保留事实与独立的敏感标记。"""
from __future__ import annotations
from tests.model_fixtures import saved_outcome

from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine
from mnemotrace.consolidate.outcomes import OUTCOME_CATEGORY
from mnemotrace.consolidate.structured import build_structured_profile
from mnemotrace.types import Fact


@pytest.mark.parametrize(("predicate", "obj", "topic", "expected"), [
    ("prefers", "茶", None, "其他"),
    ("favorite_music", "茶", "food", "饮食"),
    ("prefers", "流行的编程框架", "technology", "技术"),
    ("prefers", "课程评分", "learning", "学习"),
    ("allergic_to", "花生", "food", "健康禁忌"),
])
def test_profile_groups_by_stored_topic_and_preserves_explicit_restrictions(predicate, obj, topic, expected):
    mem = MemoryEngine()
    mem.add_fact("user", predicate, obj, topic=topic)
    assert list(mem.structured_profile()["preferences"]) == [expected]


@pytest.mark.parametrize(("predicate", "obj", "expected"), [
    ("prefers", "team collaboration", "其他"),
    ("prefers", "流行的编程框架", "其他"),
    ("likes_music", "tea", "音乐"),
    ("allergic_to", "花生", "健康禁忌"),
])
def test_profile_groups_without_rewriting_fact(predicate, obj, expected):
    fact = Fact("user", predicate, obj, source="user", category="人工分类", sensitive=True,
                provenance=["synthetic-episode"], supersedes="synthetic-previous",
                topic="music" if predicate == "likes_music" else "unknown",
                topic_source="user" if predicate == "likes_music" else "unset")
    before = asdict(fact)
    profile = build_structured_profile([fact], "user")
    item, = profile["preferences"][expected]
    assert item["category"] == expected and item["fact_id"] == fact.id
    assert item["object"] == obj and profile["counts"]["preferences"] == 1
    assert asdict(fact) == before


def test_manual_writes_default_to_sensitive_without_inferred_category():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "Storage Labs")
    assert (fact.category, fact.sensitive) == ("其他", True)
    mem.update_fact(fact.id, predicate="uses", object="storage engine")
    assert fact.category == "其他"
    mem.update_fact(fact.id, predicate="prefers", object="team collaboration")
    assert fact.category == "其他"
    item, = mem.structured_profile()["preferences"]["其他"]
    assert item["fact_id"] == fact.id

    manual = mem.add_fact("user", "uses", "storage engine", category="人工分类", sensitive=True)
    assert (manual.category, manual.sensitive) == ("人工分类", True)
    mem.update_fact(manual.id, category="另一分类", sensitive=False)
    assert (manual.category, manual.sensitive) == ("另一分类", False)


def test_offline_extraction_stores_model_category():
    mem = MemoryEngine()
    episode = mem.add("I work at Storage Labs.", user_id="synthetic-user")
    mem.consolidate()
    facts = [f for f in mem.fact_store.values() if episode.id in f.provenance]
    assert len(facts) == 1
    assert facts[0].predicate == "works_at" and facts[0].category == "工作"


@pytest.mark.parametrize("subject", ["user", "Bank of China", "Health Center", "Age Lab"])
def test_subject_does_not_change_category_on_add_or_edit(subject):
    mem = MemoryEngine()
    fact = mem.add_fact(subject, "uses", "Vim")
    assert fact.category == "其他"
    mem.update_fact(fact.id, object="Neovim")
    assert fact.category == "其他"


def test_existing_category_and_metadata_edits_do_not_reclassify():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "Storage Labs", category="身份")
    mem.update_fact(fact.id, sensitive=False)
    assert fact.category == "身份"
    mem.update_fact(fact.id, object="Other Storage Labs")
    assert (fact.category, fact.sensitive) == ("身份", True)


@pytest.mark.parametrize(("predicate", "obj", "expected", "sensitive"), [
    ("allergic_to", "花生", "健康禁忌", True),
    ("allergic", "peanuts", "健康禁忌", True),
    ("cannot_eat", "花生", "健康禁忌", False),
    ("cant_eat", "peanuts", "健康禁忌", False),
    ("prefers", "喜欢过敏知识书籍", "其他", True),
    ("prefers", "allergy books", "其他", True),
    ("prefers", "过敏", "其他", True),
    ("prefers", "lactose intolerance", "其他", False),
    ("dietary_restriction", "peanuts", "其他", False),
    ("dietary_restriction", "seafood", "其他", False),
    ("favorite_allergy", "books", "其他", True),
])
def test_profile_restrictions_require_explicit_relation(predicate, obj, expected, sensitive):
    mem = MemoryEngine()
    fact = mem.add_fact("user", predicate, obj, sensitive=sensitive)
    before = asdict(fact)
    item, = mem.structured_profile()["preferences"][expected]
    assert item["fact_id"] == fact.id
    assert fact.sensitive is sensitive
    assert asdict(fact) == before


@pytest.mark.parametrize("predicate", [" ALLERGIC--TO ", "cannot  eat", "cant__eat", "ALLERGIC"])
def test_normalized_restriction_relations_reach_profile(predicate):
    fact = Fact("user", predicate, "peanuts")
    profile = build_structured_profile([fact], "user")
    item, = profile["preferences"]["健康禁忌"]
    assert item["fact_id"] == fact.id


def test_unknown_restriction_spelling_is_not_inferred():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "allic", "peanuts")
    assert (fact.category, fact.sensitive) == ("其他", True)
    assert mem.structured_profile()["preferences"] == {}


def test_outcome_reason_stays_sensitive_after_content_edit():
    mem = MemoryEngine()
    fact = saved_outcome(mem, user_id="default", session_id="synthetic-session", kind="lesson",
                         statement="Choose the synthetic deployment option", why="The password was SYNTHETIC_VALUE")
    assert (fact.category, fact.sensitive) == (OUTCOME_CATEGORY, True)
    mem.update_fact(fact.id, statement="Choose the revised synthetic deployment option")
    assert "SYNTHETIC_VALUE" in fact.text
    assert (fact.category, fact.sensitive) == (OUTCOME_CATEGORY, True)
