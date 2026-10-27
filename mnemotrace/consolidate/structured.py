"""L2 结构化档案——在存活 fact 之上的有类型、分组视图（AGENTS.md 「架构概览与代码导航」，结构化层）。

设计约束（我们商定的"合理版本"）：
  * 派生而来，不是独立的真相源。从存活 fact 集只读构建，因此继承 provenance，
    永远不会与双时间轴存储产生偏差。
  * 仅用于展示的分层。把条目分成 `confirmed` 与 `tentative` 纯粹为了呈现
    （"might like jazz" 这种站不住的一次性猜测不会出现在规范档案里）。
    这绝不过滤检索——search()/lean_context() 仍然看到每一条 fact，召回不受影响。
  * 不编造权重。每个条目携带诚实的证据描述（你设置的 / 提到过 N 次），
    而不是虚构的 0.0–1.0 分数。用户能真正推理的置信度。

升级为 `confirmed` 的条件是证据扎实：用户亲自断言、显式的 favorite/过敏、访问时被强化过，
或在 ≥2 个独立会话中陈述过。
"""
from __future__ import annotations

from ..localize import display_of
from ..types import Fact
from ._rule_match import normalize_predicate
from ._topic_keywords import RESTRICTION_PREDICATES

# predicate -> (规范字段键, 展示标签)。身份/基本信息槽位（单值）。
_BASIC: dict[str, tuple[str, str]] = {
    "name": ("name", "姓名"),
    "age": ("age", "年龄"), "age_range": ("age", "年龄"),
    "gender": ("gender", "性别"), "sex": ("gender", "性别"),
    "occupation": ("occupation", "职业"), "job": ("occupation", "职业"),
    "profession": ("occupation", "职业"), "works_as": ("occupation", "职业"),
    "works_at": ("employer", "工作单位"), "employer": ("employer", "工作单位"),
    "company": ("employer", "工作单位"),
    "lives_in": ("home", "常住地"), "home": ("home", "常住地"),
    "based_in": ("home", "常住地"), "lives_at": ("home", "常住地"),
    "born_in": ("birthplace", "出生地"), "birthplace": ("birthplace", "出生地"),
    "birthday": ("birthday", "生日"), "born_on": ("birthday", "生日"), "birth_date": ("birthday", "生日"),
    "married_to": ("spouse", "配偶"), "spouse": ("spouse", "配偶"),
    "has_child": ("children", "子女"), "has_children": ("children", "子女"),
    "children": ("children", "子女"), "kids": ("children", "子女"),
    "nationality": ("nationality", "国籍"),
    "speaks": ("language", "语言"), "language": ("language", "语言"),
    "education": ("education", "教育"), "studied_at": ("education", "教育"),
    "graduated_from": ("education", "教育"), "degree": ("education", "教育"),
}

_POSITIVE = {"likes", "like", "loves", "love", "enjoys", "enjoy", "prefers", "prefer", "favorite",
             "favourite", "interested_in", "fan_of", "into", "fond_of", "wants", "wishes_for"}
_DIETARY = {"diet", "dietary_restriction", "dietary_restrictions"}
_NEGATIVE = {"dislikes", "dislike", "hates", "hate", "avoids", "avoid", "allergic_to", "allergic",
             "cannot_eat", "cant_eat", "not_into", "disinterested_in"}
_HABIT = {"usually", "often", "routine", "regularly", "habit", "tends_to", "commutes", "frequently",
          "daily", "weekly", "every_day", "every_week", "every_morning"}

# 这里只把已存主题映射为展示分组，不在读取时重新判断主题。
_GROUP_LABELS = {"food": "饮食", "travel": "出行", "music": "音乐", "film_tv": "影视",
                 "sports": "运动", "reading": "阅读", "hobbies": "兴趣", "environment": "环境",
                 "health": "健康", "work": "工作", "learning": "学习", "technology": "技术",
                 "finance": "财务", "relationships": "人际", "unknown": "其他"}
_GROUP_ORDER = list(_GROUP_LABELS.values())
_GROUP_ORDER.insert(_GROUP_ORDER.index("健康"), "健康禁忌")


_POS_PREFIX = ("favorite", "favourite", "likes_", "loves_", "enjoys_", "prefers_", "prefer_",
               "interested", "fond_", "fan_")
_NEG_PREFIX = ("dislikes_", "dislike_", "hates_", "avoids_", "avoid_", "allergic", "cannot_",
               "doesn't_", "does_not_", "do_not_", "not_", "no_")


def _polarity(pred: str) -> str | None:
    """偏好 predicate 的 like / dislike 极性——处理 LLM 产出的复合 predicate
    （likes_quiet_environments、doesn't_drive_on_elevated_roads 等），不止规范动词。"""
    p = normalize_predicate(pred)
    if p in _DIETARY:
        return "diet"
    if p in _POSITIVE or p.startswith(_POS_PREFIX):
        return "like"
    if p in _NEGATIVE or p.startswith(_NEG_PREFIX):
        return "dislike"
    return None


def _is_habit(pred: str) -> bool:
    p = pred.lower()
    return p in _HABIT or any(p.startswith(h + "_") for h in _HABIT)


def _preference_group(fact: Fact) -> str:
    # 禁忌是明确关系的独立展示特例，不能仅凭 health 主题推断。
    if normalize_predicate(fact.predicate) in RESTRICTION_PREDICATES:
        return "健康禁忌"
    return _GROUP_LABELS[fact.topic]


def _evidence(f: Fact) -> dict:
    """来源数统计不同的 Episode ID，强化次数独立保留检索使用次数。"""
    source_count = len(set(f.provenance))
    if f.source == "user":
        kind = "user"
    elif f.access_count > 0 and source_count < 2:
        kind = "reinforced"
    else:
        kind = "mentions"
    return {"kind": kind, "source_count": source_count, "reinforcement_count": f.access_count}


def _confirmed(f: Fact) -> bool:
    """偏好是进入规范档案，还是作为 待确认 候选存在（仅展示，绝不妨碍检索）。
    显式陈述的偏好即 confirmed——用户亲口说的，不是摇摆的推断（PRD 把 主动写入 视为置信度 1.0）。
    待确认 留给真正弱的信号（例如未来被动推断路径给出低于 1.0 的置信度）。"""
    if f.source == "user":
        return True
    if _polarity(f.predicate) is not None:  # 显式陈述的 喜欢/讨厌/最爱/过敏
        return True
    if len(set(f.provenance)) >= 2 or f.access_count >= 1:  # 跨会话得到印证 / 已被强化
        return True
    return False


# 通用的第一人称/用户指称（英文 + 中文共指）。它们指代用户本人——不是特例，而是每个记忆系统
# 都需要的基础身份消解。关于他人的 fact（儿子/外婆/朋友 ...）根本不在这个集合里，因此无需任何
# 亲属词表或猜测即可排除。
USER_REFS = {"user", "用户", "i", "我", "me", "myself", "本人", "自己", "俺"}


def user_aliases(subject: str, user_id: str) -> set[str]:
    """指代用户的 subject：消解出的本人称呼（如 李雷）+ user_id + USER_REFS。
    依赖提取阶段已把第一人称 subject 归一到本人称呼（见 LLMExtractor）。"""
    return {subject.lower(), user_id.lower()} | USER_REFS


def build_structured_profile(facts: list[Fact], subject: str, user_id: str = "default") -> dict:
    """把用户存活的 fact 分组为基本信息 / 无权重偏好 / 习惯，并按 confirmed 与 tentative
    拆分展示。`facts` 应当已经是存活集。"""
    who = user_aliases(subject, user_id)
    mine = [f for f in facts if f.subject.lower() in who]

    basic: dict[str, dict] = {}
    prefs: dict[str, list] = {}
    habits: list[dict] = []
    tentative: list[dict] = []

    for f in mine:
        pred = f.predicate.lower()
        # 1) 基本身份槽位（单值：每个字段保留证据最强的一条）
        if pred in _BASIC:
            field, label = _BASIC[pred]
            cand = {"field": field, "label": label, "value": f.object,
                    "evidence": _evidence(f), "source": f.source, "fact_id": f.id}
            cur = basic.get(field)
            if cur is None or _rank(f) > cur["_rank"]:
                cand["_rank"] = _rank(f)
                basic[field] = cand
            continue
        # 2) 偏好（带极性标记），消费已存主题；未知不回退到关键词。
        pol = _polarity(pred)
        if pol is not None:
            item = {"item": f.object, "polarity": pol, "category": _preference_group(f),
                    "topic": f.topic, "topic_source": f.topic_source,
                    "evidence": _evidence(f), "source": f.source, "fact_id": f.id,
                    "subject": f.subject, "predicate": f.predicate, "object": f.object}
            if _confirmed(f):
                prefs.setdefault(item["category"], []).append(item)
            else:
                tentative.append(item)
            continue
        # 3) 习惯/常规（轻量）
        if _is_habit(pred):
            habits.append({"text": display_of(f), "evidence": _evidence(f), "fact_id": f.id})

    # 用户姓名被消解进身份（本人称呼）而不是作为 fact 存储，所以在拿到它时
    # （且尚无 name fact 提供时）显式地放入 基本信息。
    if "name" not in basic and subject and subject.lower() not in (USER_REFS | {user_id.lower()}):
        basic["name"] = {"field": "name", "label": "姓名", "value": subject,
                         "evidence": {"kind": "mentions", "source_count": 0,
                                      "reinforcement_count": 0},
                         "source": "extracted", "fact_id": ""}
    for b in basic.values():
        b.pop("_rank", None)
    return {
        "basic": list(basic.values()),
        "preferences": {group: prefs[group] for group in _GROUP_ORDER if group in prefs},
        "habits": habits,
        "tentative": tentative,
        "counts": {
            "basic": len(basic),
            "preferences": sum(len(v) for v in prefs.values()),
            "tentative": len(tentative),
            "habits": len(habits),
        },
    }


def _rank(f: Fact) -> tuple:
    """为单值槽位挑选最可信的 fact：用户断言优先，其次印证最多，最后 valid 时间最近。"""
    return (1 if f.source == "user" else 0, len(set(f.provenance)), f.valid_at)
