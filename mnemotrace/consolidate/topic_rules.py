"""写入时的主题判定与模型主题协议；不读取画像、不改变事实或敏感性。"""
from __future__ import annotations

import logging

from ..topics import TOPICS, Topic, TopicSource
from ._rule_match import compile_keywords, matches_keywords, normalize_predicate
from ._topic_keywords import LIFE_TOPIC_KEYWORDS, RESTRICTION_PREDICATES

_LOG = logging.getLogger(__name__)
_RELATIONS = {
    "work": ("works_at", "employer", "occupation", "job", "profession"),
    "learning": ("studied_at", "graduated_from", "education", "degree"),
    "health": ("has_disease", "medical_condition", "medication", "takes_medication", "blood_type",
               *RESTRICTION_PREDICATES),
    "finance": ("salary", "income", "wage", "bank_account", "credit_card", "debt", "mortgage",
                "net_worth", "invests_in"),
    "relationships": ("married_to", "spouse", "has_child", "has_children", "friends_with"),
}
_RELATION_TOPICS = {relation: topic for topic, relations in _RELATIONS.items() for relation in relations}
_PREFERENCE_PREFIXES = (
    "like", "likes", "love", "loves", "enjoy", "enjoys", "prefer", "prefers", "favorite", "favourite",
    "interested_in", "fan_of", "into", "fond_of", "wants", "wishes_for", "dislike", "dislikes",
    "hate", "hates", "avoid", "avoids", "not_into", "disinterested_in", "doesn't", "does_not",
    "do_not", "not", "no", "does_not_like",
)
_SUFFIX_TOPICS = {topic: topic for topic in TOPICS if topic != "unknown"}
_SUFFIX_TOPICS.update({
    "movie": "film_tv", "movies": "film_tv", "film": "film_tv", "films": "film_tv", "tv": "film_tv",
    "book": "reading", "books": "reading", "foods": "food", "drink": "food", "drinks": "food",
    "cuisine": "food", "cuisines": "food", "restaurant": "food", "restaurants": "food",
    "sport": "sports", "hotel": "travel", "hotels": "travel", "destination": "travel",
    "destinations": "travel", "quiet_environment": "environment", "quiet_environments": "environment",
})

# 原有生活词表中的宽泛线索不一定构成主题证据：股票归财务，技术运行环境归技术。
# 过滤后的词表只用于写入判断，画像读取直接消费已存主题。
_AUTO_TOPIC_EXCLUDED = {"reading": {"股票"}, "environment": {"environment", "environments", "环境"},
                        "music": {"artist", "artists", "genre", "genres"}}
_EXTRA_RULES = (
    ("food", ("黑茶", "普洱", "白茶", "龙井", "披萨", "寿司")),
    ("health", ("疾病", "过敏", "服药", "吃药", "住院", "健康", "糖尿病", "怀孕", "孕妇",
                "disease", "diseases", "allergy", "allergies", "medication", "diabetes", "pregnancy")),
    ("work", ("工作", "职场", "雇主", "职业", "工作项目", "项目管理", "employer", "workplace")),
    ("learning", ("学习", "课程", "培训", "教育", "education", "course", "courses", "studying")),
    ("technology", ("编程", "代码", "软件", "数据库", "算法", "运行环境", "编程环境",
                    "programming", "coding", "software", "database", "databases", "algorithm",
                    "algorithms", "storage_engine", "python", "sql", "runtime_environment")),
    ("finance", ("工资", "薪资", "收入", "存款", "负债", "信用卡", "股票", "理财", "财务",
                 "salary", "income", "debt", "bank_account", "credit_card", "investment", "investments")),
    ("relationships", ("配偶", "伴侣", "家人", "亲属", "同事", "spouse", "family", "colleague")),
    ("music", ("jazz", "classical_music", "pop_music", "rock_music")),
    ("sports", ("swim", "swimming", "swimming_pool")),
)
_CONTENT_MATCHERS = tuple(
    (topic, compile_keywords(tuple(word for word in words if word not in _AUTO_TOPIC_EXCLUDED.get(topic, ()))))
    for topic, words in LIFE_TOPIC_KEYWORDS
) + tuple((topic, compile_keywords(words)) for topic, words in _EXTRA_RULES)
_HOTEL = compile_keywords(("酒店", "hotel", "hotels"))

_DESCRIPTIONS = {
    "food": "日常饮食与口味", "travel": "出行、住宿与游览", "music": "音乐",
    "film_tv": "电影、电视与影视", "sports": "运动", "reading": "阅读、书籍与播客内容消费",
    "hobbies": "其他兴趣活动", "environment": "物理环境和舒适性，不含技术运行环境",
    "health": "健康相关内容；不证明本人有疾病或禁忌", "work": "职业、组织与工作安排",
    "learning": "学习、课程与培训", "technology": "具体技术问题、方案及编程运行环境",
    "finance": "财务与金融", "relationships": "人际关系", "unknown": "无充分依据或主题难以确定",
}
_TOPIC_PROTOCOL = (
    "\n\n附加主题协议：保留原有 JSON 结构和字段，每条长期事实（facts 数组）或会话结论额外提供一个 topic 字段；working 条目不添加主题。\n"
    + "；".join(f"{topic}={_DESCRIPTIONS[topic]}" for topic in TOPICS)
    + "。\ntopic 必须精确使用一个上述英文标识，不确定时返回 unknown。按本条断言关注的内容判断，"
      "不要仅凭主语名称或整段会话主题贴标签，不推断新的事实。"
      "不要输出 topic_source、source、category 或 sensitive，它们由系统独立决定。"
)


def topic_system(system: str, enabled: bool) -> str:
    return system + _TOPIC_PROTOCOL if enabled else system


def log_topic_inputs(items: list[dict]) -> None:
    """单次属性或结论抽取调用内聚合，非法主题至多一条 WARNING。

    不跨 episode 或 session 调用累计；只记录数量，不带入模型字段、正文或用户标识。
    """
    invalid = sum("topic" in item and (not isinstance(item["topic"], str) or item["topic"] not in TOPICS)
                  for item in items)
    if invalid:
        _LOG.warning("LLM topic fallback: invalid=%d", invalid)
    _LOG.debug("LLM topic inputs: missing=%d, abstained=%d",
               sum("topic" not in item for item in items),
               sum(item.get("topic") == "unknown" for item in items))


def resolve_topic(predicate: str, obj: str, llm_topic: object = None) -> tuple[Topic, TopicSource]:
    """调用方提供最终内容；不使用 Fact.text，防止主语或会话 ID 污染规则。"""
    p = normalize_predicate(predicate)
    explicit = _RELATION_TOPICS.get(p)
    if explicit is None:
        for prefix in _PREFERENCE_PREFIXES:
            if p.startswith(prefix + "_"):
                explicit = _SUFFIX_TOPICS.get(p[len(prefix) + 1:])
                if explicit is not None:
                    break
    if explicit is not None:
        return explicit, "rule"
    if isinstance(llm_topic, str) and llm_topic in TOPICS:
        return ("unknown", "unset") if llm_topic == "unknown" else (llm_topic, "llm")
    hits = {topic for topic, words in _CONTENT_MATCHERS if matches_keywords(obj, words)}
    if len(hits) == 1:
        return hits.pop(), "rule"
    if hits == {"travel", "sports"} and matches_keywords(obj, _HOTEL):
        return "travel", "rule"
    return "unknown", "unset"
