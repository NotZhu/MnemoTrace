"""分类与画像分组使用语言边界，且不改变事实及独立的敏感标记。"""
from __future__ import annotations

from dataclasses import asdict

import pytest

from mnemotrace import Memory
from mnemotrace.consolidate.classify import classify, classify_fact
from mnemotrace.consolidate.outcomes import OUTCOME_CATEGORY, extract_outcomes
from mnemotrace.consolidate.structured import build_structured_profile
from mnemotrace.types import Episode, Fact


@pytest.mark.parametrize(("predicate", "obj", "text", "expected"), [
    ("uses", "storage engine", "", "其他"),
    ("uses", "language model", "", "其他"),
    ("uses", "myagecounter", "", "其他"),
    ("uses", "StorageEngine", "", "其他"),
    ("note", "整理参考资料和原始对话片段", "", "其他"),
    ("note", "研究方法", "", "其他"),
    ("works_at", "Storage Labs", "", "工作"),
    ("WORKS_AT", "Health Bank", "", "工作"),
    ("works-at", "Age Research", "", "工作"),
    ("likes", "健康播客", "", "偏好"),
    ("prefers", "salary reports", "", "偏好"),
    ("dislikes", "bank queues", "", "偏好"),
    ("likes_health_podcasts", "synthetic-option", "", "偏好"),
    ("favorite_music", "synthetic-option", "", "偏好"),
    ("age", "24", "", "身份"),
    ("age_range", "20-29", "", "身份"),
    ("has_disease", "diabetes", "", "健康"),
    ("allergic_to", "seafood", "", "健康"),
    ("lives_in", "Bank District", "", "位置"),
    ("married_to", "Synthetic Name", "", "关系"),
    ("visited", "Health Museum", "", "事件"),
    ("note", "AGE: 24", "", "身份"),
    ("note", "blood-type: O", "", "健康"),
    ("note", "credit_card", "", "财务"),
    ("note", "medical diagnosis", "", "健康"),
    ("note", "pregnancy", "", "健康"),
    ("note", "disabilities", "", "健康"),
    ("note", "investments", "", "财务"),
    ("note", "疾病和药物", "", "健康"),
    ("note", "糖尿病", "", "健康"),
    ("note", "synthetic-option", "用户年龄是24岁", "其他"),
    ("note", "", "", "其他"),
])
def test_fact_category_boundaries_and_predicate_precedence(predicate, obj, text, expected):
    assert classify(predicate, obj, text)[0] == expected


@pytest.mark.parametrize(("predicate", "obj", "expected"), [
    ("prefers", "team collaboration", "其他"),
    ("prefers", "teamwork", "其他"),
    ("prefers", "thread management", "其他"),
    ("prefers", "showcase code", "其他"),
    ("prefers", "bandwidth monitoring", "其他"),
    ("prefers", "整理参考资料", "其他"),
    ("prefers", "流行的编程框架", "技术"),
    ("prefers", "保留事实和原始对话片段", "其他"),
    ("prefers", "秘书协作", "其他"),
    ("prefers", "课程评分", "学习"),
    ("prefers", "tea", "饮食"),
    ("prefers", "TEA, please!", "饮食"),
    ("prefers", "tea2", "其他"),
    ("prefers", "black_tea", "饮食"),
    ("prefers", "茶", "饮食"),
    ("prefers", "喝绿茶", "饮食"),
    ("prefers", "奶茶", "饮食"),
    ("prefers", "川菜", "饮食"),
    ("prefers", "吃辣", "饮食"),
    ("prefers", "食材", "饮食"),
    ("prefers", "烹饪调料", "饮食"),
    ("prefers", "cooking spicy dishes", "饮食"),
    ("prefers", "流行音乐", "音乐"),
    ("prefers", "pop music", "音乐"),
    ("prefers", "songs", "音乐"),
    ("prefers", "电影", "影视"),
    ("prefers", "纪录片", "影视"),
    ("prefers", "movies", "影视"),
    ("prefers", "游泳", "运动"),
    ("prefers", "running", "运动"),
    ("prefers", "books", "阅读"),
    ("prefers", "阅读小说", "阅读"),
    ("prefers", "有游泳池的酒店", "出行"),
    ("favorite_music", "周杰伦", "音乐"),
    ("FAVOURITE_MOVIES", "Synthetic Title", "影视"),
    ("favorite_movie", "Inception", "影视"),
    ("favorite_book", "tea history", "阅读"),
    ("likes_music", "tea", "音乐"),
    ("prefers_quiet_environments", "music", "环境"),
    ("allergic_to", "花生", "健康禁忌"),
    ("allergic_to", "synthetic hotel", "健康禁忌"),
    ("prefers", "lactose intolerance", "其他"),
    ("prefers", "synthetic-option", "其他"),
])
def test_profile_grouping_boundaries_and_predicate_precedence(predicate, obj, expected):
    mem = Memory()
    mem.add_fact("user", predicate, obj)
    assert list(mem.structured_profile()["preferences"]) == [expected]


@pytest.mark.parametrize(("predicate", "obj", "text", "expected"), [
    ("salary", "25000", "", True),
    ("has_disease", "diabetes", "", True),
    ("password", "synthetic-value", "", True),
    ("note", "123456", "", True),
    ("note", "synthetic-value", "contains a secret", True),
    # 敏感规则仍独立使用原有词干/子串策略；分类修复不能静默改变它。
    ("uses", "fundamental techniques", "", True),
    ("uses", "storage engine", "", False),
    ("works_at", "Acme", "", False),
])
def test_sensitivity_is_independent_of_category_matching(predicate, obj, text, expected):
    assert classify(predicate, obj, text)[1] is expected


def test_classify_fact_preserves_explicit_category_and_sensitive_marking():
    manual = Fact("user", "uses", "storage engine", category="人工分类", sensitive=True)
    before = asdict(manual)
    classify_fact(manual)
    assert asdict(manual) == before

    unclassified = Fact("user", "uses", "storage engine", sensitive=True)
    classify_fact(unclassified)
    assert (unclassified.category, unclassified.sensitive) == ("其他", True)


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
                topic_source="rule" if predicate == "likes_music" else "unset")
    before = asdict(fact)
    profile = build_structured_profile([fact], "user")
    item, = profile["preferences"][expected]
    assert item["category"] == expected and item["fact_id"] == fact.id
    assert item["object"] == obj and profile["counts"]["preferences"] == 1
    assert asdict(fact) == before


def test_memory_writes_and_content_edits_use_category_rules():
    mem = Memory()
    fact = mem.add_fact("user", "works_at", "Storage Labs")
    assert fact.category == "工作"
    mem.update_fact(fact.id, predicate="uses", object="storage engine")
    assert fact.category == "其他"
    mem.update_fact(fact.id, predicate="prefers", object="team collaboration")
    assert fact.category == "偏好"
    item, = mem.structured_profile()["preferences"]["其他"]
    assert item["fact_id"] == fact.id

    manual = mem.add_fact("user", "uses", "storage engine", category="人工分类", sensitive=True)
    assert (manual.category, manual.sensitive) == ("人工分类", True)
    mem.update_fact(manual.id, category="另一分类", sensitive=False)
    assert (manual.category, manual.sensitive) == ("另一分类", False)


def test_offline_extraction_uses_category_rules():
    mem = Memory()
    episode = mem.add("I work at Storage Labs.", user_id="synthetic-user")
    mem.consolidate()
    facts = [f for f in mem.fact_store.values() if episode.id in f.provenance]
    assert len(facts) == 1
    assert facts[0].predicate == "works_at" and facts[0].category == "工作"


@pytest.mark.parametrize(("obj", "expected"), [
    ("吃饭", "饮食"), ("吃外卖", "饮食"), ("点外卖", "饮食"), ("做菜", "饮食"),
    ("早餐", "饮食"), ("午餐", "饮食"), ("晚餐", "饮食"),
    ("早饭", "饮食"), ("午饭", "饮食"), ("晚饭", "饮食"),
    ("菜谱", "饮食"), ("菜单", "饮食"), ("外卖", "饮食"), ("麻辣烫", "饮食"), ("乌龙茶", "饮食"),
    ("羽毛球", "运动"), ("打羽毛球", "运动"), ("网球", "运动"), ("打网球", "运动"),
    ("乒乓球", "运动"), ("打乒乓球", "运动"), ("排球", "运动"), ("打排球", "运动"),
    ("看书", "阅读"), ("书店", "阅读"), ("看小说", "阅读"),
    ("听歌", "音乐"), ("唱歌", "音乐"), ("短片", "影视"),
    ("秘书", "其他"), ("地球仪", "其他"), ("曲线", "其他"), ("诗歌", "其他"),
    ("照片", "其他"), ("书桌", "其他"), ("课程评分", "学习"),
])
def test_profile_common_chinese_expressions(obj, expected):
    mem = Memory()
    mem.add_fact("user", "prefers", obj)
    profile = mem.structured_profile()
    item, = profile["preferences"][expected]
    assert item["item"] == obj


@pytest.mark.parametrize(("obj", "expected"), [
    ("孕妇", "健康"), ("在服药", "健康"), ("服药中", "健康"), ("吃药", "健康"), ("住院", "健康"),
    ("去了杭州", "事件"), ("买水果", "事件"), ("住北京", "位置"),
    ("去重算法", "其他"), ("买卖协议", "其他"), ("住宿预订", "其他"),
    ("秘书", "其他"), ("地球仪", "其他"), ("曲线", "其他"), ("诗歌", "其他"),
    ("照片", "其他"), ("书桌", "其他"), ("课程评分", "其他"),
])
def test_fact_common_chinese_expressions(obj, expected):
    assert classify("note", obj)[0] == expected


@pytest.mark.parametrize("subject", ["user", "Bank of China", "Health Center", "Age Lab"])
def test_subject_does_not_change_category_on_add_or_edit(subject):
    mem = Memory()
    fact = mem.add_fact(subject, "uses", "Vim")
    assert fact.category == "其他"
    mem.update_fact(fact.id, object="Neovim")
    assert fact.category == "其他"


@pytest.mark.parametrize("predicate", ["uses_age_api", "research_health", "bank_connector"])
def test_unknown_predicate_fragments_are_not_category_evidence(predicate):
    assert classify(predicate, "Vim")[0] == "其他"


def test_fact_body_still_marks_sensitive_without_changing_category():
    fact = Fact("Age Lab", "note", "synthetic-option",
                text="My password is SYNTHETIC_VALUE")
    classify_fact(fact)
    assert (fact.category, fact.sensitive) == ("其他", True)


def test_existing_category_and_metadata_edits_do_not_reclassify():
    mem = Memory()
    fact = mem.add_fact("user", "works_at", "Storage Labs", category="身份")
    classify_fact(fact)
    mem.update_fact(fact.id, sensitive=False)
    assert fact.category == "身份"
    mem.update_fact(fact.id, object="Other Storage Labs")
    assert fact.category == "工作"


@pytest.mark.parametrize(("predicate", "obj", "expected", "sensitive"), [
    ("allergic_to", "花生", "健康禁忌", True),
    ("allergic", "peanuts", "健康禁忌", True),
    ("cannot_eat", "花生", "健康禁忌", False),
    ("cant_eat", "peanuts", "健康禁忌", False),
    ("prefers", "喜欢过敏知识书籍", "其他", True),
    ("prefers", "allergy books", "其他", True),
    ("prefers", "过敏", "健康", True),
    ("prefers", "lactose intolerance", "其他", False),
    ("dietary_restriction", "peanuts", "其他", False),
    ("dietary_restriction", "seafood", "饮食", False),
    ("favorite_allergy", "books", "阅读", True),
])
def test_profile_restrictions_require_explicit_relation(predicate, obj, expected, sensitive):
    mem = Memory()
    fact = mem.add_fact("user", predicate, obj)
    before = asdict(fact)
    item, = mem.structured_profile()["preferences"][expected]
    assert item["fact_id"] == fact.id
    assert fact.sensitive is sensitive
    assert asdict(fact) == before


@pytest.mark.parametrize("stem", ["medical_condition", "health_condition"])
@pytest.mark.parametrize("plural", ["", "s"])
@pytest.mark.parametrize("separator", [" ", "  ", "\t", "_", "-", "_ -\t"])
def test_medical_phrases_accept_equivalent_separators(stem, plural, separator):
    obj = (stem + plural).replace("_", separator)
    assert classify("note", f"({obj.upper()})")[0] == "健康"


@pytest.mark.parametrize(("obj", "expected"), [
    ("conditions apply", "其他"), ("condition", "其他"),
    ("medical_conditionally", "其他"), ("premedical_condition", "其他"),
    ("blood type: O", "健康"), ("blood  type: O", "健康"),
    ("credit-card", "财务"), ("credit  card", "财务"),
])
def test_ambiguous_conditions_and_separator_boundaries(obj, expected):
    assert classify("note", obj)[0] == expected


@pytest.mark.parametrize("predicate", [" ALLERGIC--TO ", "cannot  eat", "cant__eat", "ALLERGIC"])
def test_normalized_restriction_relations_reach_profile(predicate):
    fact = Fact("user", predicate, "peanuts")
    profile = build_structured_profile([fact], "user")
    item, = profile["preferences"]["健康禁忌"]
    assert item["fact_id"] == fact.id


def test_unknown_restriction_spelling_is_not_inferred():
    mem = Memory()
    fact = mem.add_fact("user", "allic", "peanuts")
    assert (fact.category, fact.sensitive) == ("其他", False)
    assert mem.structured_profile()["preferences"] == {}


def test_outcome_reason_stays_sensitive_after_content_edit():
    class StubLLM:
        def complete(self, *args, **kwargs):
            return ('[{"kind":"lesson","statement":"Choose the synthetic deployment option",'
                    '"why":"The password was SYNTHETIC_VALUE"}]')

    episode = Episode("Synthetic deployment discussion", user_id="default")
    fact, = extract_outcomes(StubLLM(), [episode], "default", session_id="synthetic-session")
    assert (fact.category, fact.sensitive) == (OUTCOME_CATEGORY, True)
    mem = Memory()
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    mem.update_fact(fact.id, statement="Choose the revised synthetic deployment option")
    assert "SYNTHETIC_VALUE" in fact.text
    assert (fact.category, fact.sensitive) == (OUTCOME_CATEGORY, True)
