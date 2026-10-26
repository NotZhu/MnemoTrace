"""离线、确定性的 (subject, predicate, object) 提取器。

这是零依赖的回退实现。它处理 demo 和测试用到的常见第一/第三人称句式；有意不做通用化。
生产环境在相同的 `.extract(episode) -> list[Fact]` API 后面换用 LLM 提取器（AGENTS.md 「架构概览与代码导航」, 「核心不变量」）。
保持 API 一致，离线 demo 和 GPT/Kimi/DeepSeek 驱动的运行才能共享其余整条管线。
"""
from __future__ import annotations

import re

from ..types import Episode, Fact
from .topic_rules import resolve_topic

EXPLICIT_PREFERENCE_PREDS = frozenset({"likes", "loves", "enjoys", "prefers", "dislikes", "hates", "avoids"})

# 从捕获的 object 尾部剥掉的赘词（"Moonshot AI too" -> "Moonshot AI"）
_FILLER = {"too", "now", "also", "currently", "anymore", "though", "well", "as",
           "right", "these", "days", "nowadays", "actually"}
_PRONOUNS = {"i", "my", "me", "myself", "we", "actually", "then", "and", "but", "so", "also", "now"}
_NON_OCCUPATIONS = {
    "awesome", "cool", "exciting", "fine", "fun", "good", "great", "nice", "noted", "ok", "okay",
    "wonderful",
}
_NON_NAMES = {
    "a", "an", "the", "not", "working", "going", "moving", "relocating", "into", "fond", "fan",
    "vegetarian", "particularly", "especially", "interested", "looking", "planning", "thinking",
    "trying", "getting", "glad", "happy", "sure", "always", "also", "still", "really",
}
_WEAK_PREFERENCE_OBJECTS = {
    "it", "this", "that", "these", "those", "them", "thing", "things", "stuff", "something",
    "anything", "everything", "one", "ones", "use", "process", "experience", "journey",
    "idea", "ideas", "suggestion", "suggestions", "tip", "tips", "option", "options",
}
_WEAK_DEICTIC_HEADS = ("these", "those", "this", "that")
_WEAK_DEICTIC_NOUNS = {"idea", "ideas", "suggestion", "suggestions", "tip", "tips", "option", "options"}
_PREFERENCE_OBJECT_PREFIXES = (
    r"(?:i\s+)?(?:really\s+)?(?:like|love|enjoy|prefer)s?\s+(?:the\s+)?sound of\s+",
    r"(?:the\s+)?sound of\s+",
    r"(?:i\s+)?(?:really\s+)?(?:like|love|enjoy|prefer)s?\s+(?:the\s+)?idea of\s+",
    r"(?:the\s+)?idea of\s+",
    r"(?:my|your|our|their)\s+idea of\s+",
    r"(?:the\s+)?concept of\s+",
)

_CLAUSE_SPLIT = re.compile(r"\n+|[.!?。！？]+(?:\s+|$)|,|;| and | but |—|--|\bthen\b", re.I)
_FAMILY = "sister|brother|mother|father|parent|child|spouse|wife|husband|partner"
_I_AM = r"i\s*(?:am|'m|m)?"


def _clean_obj(s: str) -> str:
    s = s.strip().strip(".!?;:, ").strip()
    toks = s.split()
    while toks and toks[-1].lower() in _FILLER:
        toks.pop()
    if toks and toks[0].lower() == "the":
        toks = toks[1:]
    return " ".join(toks)


def _clean_role(s: str) -> str:
    """保留职业/角色的主体，去掉工作场所或解释性尾巴。"""
    return _clean_obj(re.split(r"\b(?:at|for|with|in)\b", s, 1, flags=re.I)[0])


def _clean_location(s: str) -> str:
    """从移动类从句中保留目的地的主体。"""
    return _clean_obj(re.split(
        r"\b(?:next|this|last)\s+(?:week|month|year)|\b(?:for|with|after|before)\b",
        s,
        1,
        flags=re.I,
    )[0])


def _clean_procedure_steps(s: str) -> str:
    return " ".join(_clean_obj(s).split())


def _clean_preference_obj(s: str) -> str:
    text = _clean_obj(s)
    text = re.split(r"\b(?:over|rather than|instead of|because|when|while)\b", text, 1, flags=re.I)[0]
    text = re.sub(r"^(?:to|that)\s+", "", text.strip(), flags=re.I)
    return _clean_obj(text)


def normalize_preference_object(obj: str) -> str:
    text = _clean_obj(obj)
    for prefix in _PREFERENCE_OBJECT_PREFIXES:
        new = re.sub(rf"^{prefix}", "", text, flags=re.I).strip()
        if new != text:
            text = _clean_obj(new)
            break
    return text


def preference_topic_object(predicate: str, obj: str, normalize: bool) -> str:
    """主题使用整合后将落库的取值；不提前改变事实，以保留既有过滤顺序。"""
    if normalize and predicate.lower() in EXPLICIT_PREFERENCE_PREDS:
        return normalize_preference_object(obj)
    return obj


def is_specific_preference_object(obj: str) -> bool:
    text = re.sub(r"\s+", " ", obj.strip().strip("\"'`*_ ")).lower()
    if not text:
        return False
    if text in _WEAK_PREFERENCE_OBJECTS:
        return False
    if text.startswith(("you mentioned", "what you", "how they", "how it", "how this", "how that")):
        return False
    toks = text.split()
    if toks and toks[0] in _WEAK_DEICTIC_HEADS and any(t in _WEAK_DEICTIC_NOUNS for t in toks[1:]):
        return False
    return True


def preference_reversal_objects(text: str) -> set[str]:
    """高置信的偏好更新，其自然含义是"我不再喜欢这个了"。"""
    out: set[str] = set()
    for raw in _CLAUSE_SPLIT.split(text):
        clause = raw.strip()
        if not clause:
            continue
        patterns = (
            r"\b(?:i|we)\s+no\s+longer\s+(?:like|love|enjoy)s?\s+(.+)",
            rf"\b{_I_AM}\s+no\s+longer\s+(?:into|fond of|(?:an? )?fan of)\s+(.+)",
            r"\b(?:i|we)\s+stopped\s+(?:liking|loving|enjoying)\s+(.+)",
        )
        for pattern in patterns:
            m = re.search(pattern, clause, re.I)
            if not m:
                continue
            obj = _clean_preference_obj(m.group(1))
            if obj:
                out.add(obj)
            break
    return out


def is_preference_reversal_fact(fact: Fact, source_text: str) -> bool:
    if fact.predicate.lower() not in {"dislikes", "hates", "avoids"}:
        return False
    obj = normalize_preference_object(fact.object)
    return any(normalize_preference_object(candidate).lower() == obj.lower()
               for candidate in preference_reversal_objects(source_text))


def _procedure_subject(action: str) -> str:
    """从 how-to 动作中选出稳定的槽位 subject：'rotate the PAT' -> 'PAT'。"""
    text = _clean_obj(action)
    text = re.sub(
        r"^(?:how\s+to|to)\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:rotate|reset|update|deploy|install|renew|refresh|regenerate|open|create|start|stop|"
        r"restart|configure|setup|set\s+up|run|use|change|replace)\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.I).strip()
    return text or "procedure"


_PROCEDURE_ACTION = (
    r"rotate|reset|update|deploy|install|renew|refresh|regenerate|create|start|stop|restart|"
    r"configure|setup|set\s+up|change|replace|revoke|enable|disable"
)


def _slug(s: str) -> str:
    return re.sub(r"\s+", "_", s.strip().lower())


class RuleExtractor:
    def __init__(
        self,
        explicit_preference_extraction: bool = True,
        preference_object_filter: bool = True,
        preference_object_normalization: bool = True,
        preference_reversal_extraction: bool = True,
    ) -> None:
        # 按用户学习本人称呼，让 "I work at X" 能归属到 "Wei" 而不是代词。
        self.self_name: dict[str, str] = {}
        self.explicit_preference_extraction = explicit_preference_extraction
        self.preference_object_filter = preference_object_filter
        self.preference_object_normalization = preference_object_normalization
        self.preference_reversal_extraction = preference_reversal_extraction

    def extract(self, ep: Episode) -> list[Fact]:
        facts: list[Fact] = self._episode_procedures(ep)
        previous_preference = False
        for raw in _CLAUSE_SPLIT.split(ep.content):
            clause = raw.strip()
            if clause:
                if (
                    self.explicit_preference_extraction
                    and previous_preference
                    and clause[:1].islower()
                    and re.match(r"(?:like|love|enjoy|prefer|dislike|hate|avoid)s?\s+.+", clause, re.I)
                ):
                    clause = f"I {clause}"
                facts.extend(self._clause(clause, ep))
                previous_preference = self._is_first_person_preference_clause(clause)
        return facts

    def self_of(self, user_id: str) -> str:
        return self.self_name.get(user_id, user_id)

    def _first_person(self, clause: str) -> bool:
        return re.search(r"\b(i|i'm|im|i've|my|me|myself)\b", clause, re.I) is not None

    def _subject(self, clause: str, ep: Episode) -> str:
        if self._first_person(clause):
            return self.self_of(ep.user_id)
        m = re.match(r"([A-Z][\w]*)", clause.strip())
        if m and m.group(1).lower() not in _PRONOUNS:
            return m.group(1)
        return self.self_of(ep.user_id)

    def _mk(self, subj: str, pred: str, obj: str, ep: Episode) -> Fact:
        topic, topic_source = resolve_topic(
            pred, preference_topic_object(pred, obj, self.preference_object_normalization))
        return Fact(
            subject=subj,
            predicate=pred,
            object=obj,
            user_id=ep.user_id,
            valid_at=ep.event_time,
            created_at=ep.ingested_at,
            provenance=[ep.id],
            topic=topic,
            topic_source=topic_source,
        )

    def _episode_procedures(self, ep: Episode) -> list[Fact]:
        text = " ".join(ep.content.split())
        out: list[Fact] = []

        # 例句："PAT runbook source: rotate the PAT by opening settings, regenerating token,
        # then updating CI"。锚定是有意的：导入的会话含有 "assistant:"/"user:" 角色标签，
        # 宽松的冒号规则会把普通建议误升格为持久 procedure。
        m = re.search(
            r"^\s*(?P<subject>[A-Za-z0-9][A-Za-z0-9 _./-]{0,48}?)\s+"
            r"(?:runbook|procedure|workflow|checklist)(?:\s+source)?\s*:\s*(?P<steps>.+)",
            text,
            re.I,
        )
        if m:
            subject = re.sub(r"^(?:the|a|an)\s+", "", _clean_obj(m.group("subject")), flags=re.I)
            steps = _clean_procedure_steps(m.group("steps"))
            if subject and steps:
                out.append(self._mk(subject, "procedure", steps, ep))
                return out

        # 例句："To rotate the PAT: open settings, regenerate token, then update CI"
        m = re.search(
            rf"^\s*(?:to|how to)\s+(?P<action>(?:{_PROCEDURE_ACTION})\b[^:]{{0,90}})\s*:\s*(?P<steps>.+)",
            text,
            re.I,
        )
        if m:
            subject = _procedure_subject(m.group("action"))
            steps = _clean_procedure_steps(m.group("steps"))
            if subject and steps:
                out.append(self._mk(subject, "procedure", steps, ep))
        return out

    def _clause(self, clause: str, ep: Episode) -> list[Fact]:
        out: list[Fact] = []

        # 0. Codex / Claude Code 适配器使用的 agent/项目记忆约定。
        # 即使在零依赖离线模式下，也能把有意的记忆笔记变成结构化 fact。
        m = re.match(r"\s*(?:project|repo|repository)\s+(rule|preference|decision|note)\s*:\s*(.+)", clause, re.I)
        if m:
            out.append(self._mk("project", m.group(1).lower(), _clean_obj(m.group(2)), ep))
            return out

        m = re.match(r"\s*(codex|claude code|cursor|agent)\s+should\s+(.+)", clause, re.I)
        if m:
            out.append(self._mk(m.group(1), "agent_instruction", _clean_obj("should " + m.group(2)), ep))
            return out

        # 1. 搬迁 + 目的。必须先于姓名检测运行，否则对这套精简的离线提取器来说，"I'm moving..."
        # 会看起来像用户名叫 "moving"。
        m = re.search(r"\b(?:i'?m|i am|we'?re|we are)?\s*(?:moving|relocating) to (.+)", clause, re.I)
        if m:
            dest = _clean_location(m.group(1))
            if dest:
                out.append(self._mk(self._subject(clause, ep), "lives_in", dest, ep))
            job = re.search(r"\bfor (?:a |an |the )?job at (.+)", clause, re.I)
            if job:
                out.append(self._mk(self._subject(clause, ep), "works_at", _clean_obj(job.group(1)), ep))
            return out

        # 2. 饮食限制与过敏。
        m = re.search(r"\ballergic to (.+)", clause, re.I)
        if m:
            out.append(self._mk(self._subject(clause, ep), "allergic_to", _clean_obj(m.group(1)), ep))
            return out
        if re.search(r"\b(?:i'?m|i am|we'?re|we are)?\s*vegetarian\b", clause, re.I):
            out.append(self._mk(self._subject(clause, ep), "diet", "vegetarian", ep))
            return out

        # 3. name -> 只注册身份，不产出 SPO fact
        m = re.search(r"\bmy name is (\w[\w'-]*)", clause, re.I)
        if m and m.group(1).lower() not in _NON_NAMES:
            self.self_name[ep.user_id] = m.group(1)
            return out
        m = re.search(r"\bi am (\w[\w'-]*)\b", clause, re.I) or re.search(r"\bi'?m (\w[\w'-]*)\b", clause, re.I)
        if (
            m
            and m.group(1).lower() not in _NON_NAMES
            and m.group(1)[:1].isupper()
            and len(clause.split()) <= 3
        ):
            self.self_name[ep.user_id] = m.group(1)
            return out

        # 4. "my colleague Lin works at Moonshot AI" -> colleague 关系边 + Lin 的雇主
        m = re.search(r"\bmy (?:colleague|coworker|co-worker|friend) (\w+) works? (?:at|for) (.+)", clause, re.I)
        if m:
            out.append(self._mk(self.self_of(ep.user_id), "colleague", m.group(1), ep))
            out.append(self._mk(m.group(1), "works_at", _clean_obj(m.group(2)), ep))
            return out

        # 5. 单纯提到同事
        m = re.search(r"\bmy (?:colleague|coworker|co-worker|friend) (?:is )?(\w+)", clause, re.I)
        if m:
            out.append(self._mk(self.self_of(ep.user_id), "colleague", m.group(1), ep))
            return out

        # 6. "my sister Maya is a pediatrician" -> 家人关系边 + Maya 的职业
        m = re.search(rf"\bmy ({_FAMILY}) (\w+) is (?:an? |the )?(.+)", clause, re.I)
        if m:
            rel, name, role = m.group(1).lower(), m.group(2), _clean_role(m.group(3))
            out.append(self._mk(self.self_of(ep.user_id), rel, name, ep))
            if role:
                out.append(self._mk(name, "occupation", role, ep))
            return out

        # 7. 单纯提到家人
        m = re.search(rf"\bmy ({_FAMILY}) (?:is )?(\w+)", clause, re.I)
        if m:
            out.append(self._mk(self.self_of(ep.user_id), m.group(1).lower(), m.group(2), ep))
            return out

        # 8. "Maya moved to Seattle" / "Maya relocated to Seattle" -> 当前居住地
        m = re.search(r"\b(?:moved|relocated) to (.+)", clause, re.I)
        if m:
            out.append(self._mk(self._subject(clause, ep), "lives_in", _clean_location(m.group(1)), ep))
            return out

        # 9. "my favorite X is Y"（favou?rite 同时匹配 "favorite" 和 "favourite"）
        m = re.search(r"\bfavou?rite (.+?) (?:is|are) (.+)", clause, re.I)
        if m:
            out.append(self._mk(self._subject(clause, ep), "favorite_" + _slug(m.group(1)), _clean_obj(m.group(2)), ep))
            return out

        # 10. "I am a fan of jazz" / "I'm into synthwave" -> 偏好，而非职业。
        m = (
            re.search(rf"\b{_I_AM}\s+(?:an? )?fan of (.+)", clause, re.I)
            or re.search(rf"\b{_I_AM}\s+(?:really )?into (.+)", clause, re.I)
            or re.search(rf"\b{_I_AM}\s+fond of (.+)", clause, re.I)
        )
        if m:
            out.append(self._mk(self._subject(clause, ep), "likes", _clean_obj(m.group(1)), ep))
            return out

        # 11. 显式偏好动词。在不调用 LLM 的前提下补上零依赖场景对常见个人陈述
        # （"I prefer aisle seats"、"I avoid red-eyes"）的覆盖，写路径依然无 LLM。
        if self.explicit_preference_extraction:
            pref = self._explicit_preference(clause, ep)
            if pref is not None:
                out.append(pref)
                return out

        # 12. "Maya is a pediatrician" / "I work as a designer" -> 职业
        m = (
            re.search(r"\bworks? as (?:an? |the )?(.+)", clause, re.I)
            or re.search(r"\bi am (?:an? |the )(.+)", clause, re.I)
            or re.search(r"\bi'?m (?:an? |the )(.+)", clause, re.I)
            or re.search(r"\bis (?:an? |the )?(.+)", clause, re.I)
        )
        if m:
            role = _clean_role(m.group(1))
            if (
                role
                and role.lower() not in {"my", "your", "his", "her", "their", *_NON_OCCUPATIONS}
                and "fan of" not in role.lower()
            ):
                out.append(self._mk(self._subject(clause, ep), "occupation", role, ep))
                return out

        # 13. 句式："<subj> works at Y"
        m = re.search(r"\bworks? (?:at|for) (.+)", clause, re.I)
        if m:
            out.append(self._mk(self._subject(clause, ep), "works_at", _clean_obj(m.group(1)), ep))
            return out

        # 14. 句式："<subj> lives in Y"
        m = re.search(r"\blives? in (.+)", clause, re.I)
        if m:
            out.append(self._mk(self._subject(clause, ep), "lives_in", _clean_obj(m.group(1)), ep))
            return out

        return out

    def _explicit_preference(self, clause: str, ep: Episode) -> Fact | None:
        if self.preference_reversal_extraction:
            for obj in preference_reversal_objects(clause):
                if self.preference_object_normalization:
                    obj = normalize_preference_object(obj)
                if self.preference_object_filter and not is_specific_preference_object(obj):
                    return None
                return self._mk(ep.user_id, "dislikes", obj, ep) if obj else None

        # 否定的积极动词视为负面偏好 fact。
        m = re.search(
            r"\b(?:i|we)\s+(?:do\s+not|don't|dont)\s+(?:like|love|enjoy|prefer)\s+(.+)",
            clause,
            re.I,
        ) or re.search(rf"\b{_I_AM}\s+not\s+into\s+(.+)", clause, re.I)
        if m:
            obj = _clean_preference_obj(m.group(1))
            if self.preference_object_normalization:
                obj = normalize_preference_object(obj)
            if self.preference_object_filter and not is_specific_preference_object(obj):
                return None
            return self._mk(ep.user_id, "dislikes", obj, ep) if obj else None

        m = re.search(
            r"\b(?:i|we)\s+(?:try\s+to\s+|really\s+|strongly\s+)?"
            r"(like|love|enjoy|prefer|dislike|hate|avoid)s?\s+(.+)",
            clause,
            re.I,
        )
        if not m:
            return None

        verb = m.group(1).lower()
        obj = _clean_preference_obj(m.group(2))
        if self.preference_object_normalization:
            obj = normalize_preference_object(obj)
        if not obj:
            return None
        if self.preference_object_filter and not is_specific_preference_object(obj):
            return None
        pred = {
            "like": "likes",
            "love": "loves",
            "enjoy": "enjoys",
            "prefer": "prefers",
            "dislike": "dislikes",
            "hate": "hates",
            "avoid": "avoids",
        }[verb]
        return self._mk(ep.user_id, pred, obj, ep)

    def _is_first_person_preference_clause(self, clause: str) -> bool:
        if re.match(r"\s*(?:assistant|system)\s*:", clause, re.I):
            return False
        return bool(
            preference_reversal_objects(clause)
            or
            re.search(
                r"\b(?:i|we)\s+(?:do\s+not|don't|dont)\s+(?:like|love|enjoy|prefer)\s+",
                clause,
                re.I,
            )
            or re.search(
                r"\b(?:i|we)\s+(?:try\s+to\s+|really\s+|strongly\s+)?"
                r"(?:like|love|enjoy|prefer|dislike|hate|avoid)s?\s+",
                clause,
                re.I,
            )
            or re.search(rf"\b{_I_AM}\s+not\s+into\s+", clause, re.I)
        )
