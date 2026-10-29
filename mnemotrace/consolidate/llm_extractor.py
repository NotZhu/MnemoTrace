"""Model extraction with validated durable facts and transient working entries."""
from __future__ import annotations

import json
import re

from ..llm import LLM
from ..extraction import ExtractionError, ExtractionResponseError, require_llm
from ..types import Episode, Fact
from .preferences import preference_topic_object
from .topic_rules import log_topic_inputs, resolve_topic, topic_system

# 系统提示词
EXTRACT_SYSTEM = (
    "你是长期记忆系统中的精确信息提取引擎。\n"
    "从多轮对话中，提取明确陈述的、独立且具有长期价值的事实，涉及用户及其提到的人物或事物，"
    "包括身份、属性、偏好、关系、所有物、目标、计划，以及事件和发生时间。\n"
    '仅输出一个 JSON 对象，包含 facts 和 working 两个数组。facts 每个对象包含 "subject"、"predicate"、"object"、"text" 四个字段。'
    "字段名保持英文；predicate 使用简短的英文 snake_case，不得翻译成中文，"
    "例如 works_at、lives_in、favorite_color、owns、married_to、born_in、visited。\n"
    "明确、完整地提取偏好，使用 likes、dislikes、prefers、avoids、allergic_to、favorite_<thing> 等关系，"
    "例如 likes/'辛辣食物'、dislikes/'拥挤'、prefers/'靠窗座位'、allergic_to/'花生'。"
    "每项明确表达的偏好或不喜欢的事物，分别输出为一条独立事实。\n"
    "将长期操作规程、持续性指令、工作流程和操作步骤提取为独立的程序性事实，"
    'predicate 使用 "procedure" 或 "instruction"；将具体动作或步骤保留在 object 中，'
    "不要把一次性的临时意图当作长期操作规则。\n"
    "将第一人称（I、my、me、我）解析为对话中已知的用户姓名；如果姓名未知，则使用 user。"
    '用户声明姓名时，提取为 {"subject":"user","predicate":"name","object":"<姓名>"}。\n'
    "语言要求：subject 和 object 的值，包括姓名、地点、品牌、产品及自由文本，保留用户使用的语言，"
    "不要翻译。例如保留“字节跳动”，不要改为“ByteDance”。"
    "text 使用与对话相同的语言，将事实写成自然的一句话：中文对话生成中文句子，英文对话生成英文句子。"
    '例如 {"subject":"李雷","predicate":"works_at","object":"字节跳动","text":"李雷在字节跳动工作"}。\n'
    "不要推断或编造对话中未明确陈述的事实。facts 中只保留长期事实。working 中每条包含 content（临时状态原话的忠实概括）和 kind（state、intent 或 task）。混合内容分别放入两个数组；没有记忆价值时返回 {\"facts\":[],\"working\":[]}。时间词不是临时性的充分条件：今天搬家、现在就职、确诊过敏也可能具有长期价值。不要输出过期时间或 TTL。"
)

# 用户提示词模板
EXTRACT_TEMPLATE = "对话：\n{content}\n\nJSON 事实："

# 声明用户姓名/昵称的 predicate——全部注册为用户的别名，之后任何别名作为 subject 出现时都归一
# 到同一个规范身份（用户可能有多个称呼：李雷/小雷）。
_NAME_PREDICATES = {"name", "name_is", "is_named", "called", "nickname", "call_me", "preferred_name",
                    "name_preference", "称呼", "昵称"}
# 第一人称/用户指称，归一到用户的规范名（英文 + 中文共指）
_SELF_REFS = {"user", "i", "me", "myself", "用户", "我", "本人", "自己", "俺", "咱"}


def _norm_predicate(p: str) -> str:
    p = p.strip().lower().replace("-", " ").replace("/", " ")
    p = re.sub(r"\s+", "_", p).strip("_")
    return p


def parse_json(raw: str, expected: type):
    """Accept a complete JSON value, optionally wrapped in fences or prose."""
    if not isinstance(raw, str) or not raw.strip():
        raise ExtractionResponseError()
    starts = [i for i in (raw.find('{'), raw.find('[')) if i >= 0]
    start = min(starts) if starts else -1
    end = max(raw.rfind('}'), raw.rfind(']'))
    if start < 0 or end < start:
        raise ExtractionResponseError()
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError) as exc:
        raise ExtractionResponseError() from exc
    if not isinstance(data, expected):
        raise ExtractionResponseError()
    return data


def parse_json_facts(raw: str) -> list[dict]:
    """Strict array protocol used by session outcomes; [] is a successful empty result."""
    data = parse_json(raw, list)
    if not all(isinstance(item, dict) for item in data):
        raise ExtractionResponseError()
    return data


class LLMExtractor:
    """LLM 事实抽取器。"""
    def __init__(self, llm: LLM, system: str = EXTRACT_SYSTEM, template: str = EXTRACT_TEMPLATE,
                 *, llm_topics: bool = True, preference_object_normalization: bool = True) -> None:
        """初始化 LLM 事实抽取器。

        Args:
            llm: LLM。
            system: 系统提示词。
            template: 用户提示词模板。
            llm_topics: 是否启用主题抽取。
            preference_object_normalization: 是否对偏好对象进行归一化。
        """
        self.llm = llm
        self.system = system
        self.template = template
        self.llm_topics = llm_topics
        self.preference_object_normalization = preference_object_normalization
        self.instruction = ""  # 用户对事实抽取的补充要求；非空时附加到系统提示词
        self.self_name: dict[str, str] = {}  # 用户 ID -> 首次识别的规范名
        self.aliases: dict[str, set] = {}  # 用户 ID -> 已识别的姓名和昵称（小写）

    def self_of(self, user_id: str) -> str:
        return self.self_name.get(user_id, user_id)

    def _effective_system(self) -> str:
        system = self.system
        if self.instruction.strip():
            system += ("\n\n以下是用户关于记录内容的补充要求，请遵守，但仍只输出上述 JSON 对象：\n"
                       + self.instruction.strip())
        return topic_system(system, self.llm_topics)

    def extract(self, ep: Episode) -> list[Fact]:
        """Facts-only convenience API; automatic lifecycle callers must use raw_items."""
        data = self.raw_items(ep)
        if data["working"]:
            raise ValueError("Use raw_items() to retain extracted working memory")
        return self.facts_from(ep, data["facts"])

    def raw_items(self, ep: Episode) -> dict:
        """Stateless model call and complete validation, safe to fan out."""
        require_llm(self.llm)
        scope = ep.metadata.get("memory_scope", "long")
        system = self._effective_system()
        system += ("\n本次路由模式：" + scope +
                   ("。仅抽取长期事实，working 必须为空。" if scope == "long" else
                    "。同时判断长期事实与临时状态；同一段可以同时含有两者。"))
        try:
            raw = self.llm.complete(self.template.format(speaker=ep.speaker, content=ep.content), system=system)
        except Exception as exc:
            raise ExtractionError() from exc
        data = parse_json(raw, dict)
        for key in ("facts", "working"):
            if not isinstance(data.get(key), list) or not all(isinstance(i, dict) for i in data[key]):
                raise ExtractionResponseError()
        for item in data["facts"]:
            if not all(isinstance(item.get(k), str) and item[k].strip()
                       for k in ("subject", "predicate", "object")):
                raise ExtractionResponseError()
            if not _norm_predicate(item["predicate"]):
                raise ExtractionResponseError()
            if "text" in item and not isinstance(item["text"], str):
                raise ExtractionResponseError()
        for item in data["working"]:
            if (not isinstance(item.get("content"), str) or not item["content"].strip()
                    or not isinstance(item.get("kind"), str)
                    or item["kind"] not in {"state", "intent", "task"}
                    or set(item) - {"content", "kind"}):
                raise ExtractionResponseError()
        if scope == "long" and data["working"]:
            raise ExtractionResponseError()
        return data

    def facts_from(self, ep: Episode, items: list[dict]) -> list[Fact]:
        """把解析出的条目转成 Fact，并注册姓名/别名。有意依赖顺序：请按时间顺序调用，
        使规范名是用户实际最先说出的那个。"""
        facts: list[Fact] = []
        topic_items: list[dict] = []
        for item in items:
            subj = str(item.get("subject", "")).strip()
            pred = _norm_predicate(str(item.get("predicate", "")))
            obj = str(item.get("object", "")).strip()
            if not subj or not pred or not obj:
                continue
            if pred in _NAME_PREDICATES:
                # 把每个声明的姓名/昵称注册为用户别名；第一个是规范 subject。于是
                # 李雷 / 小雷 / 我 / 用户 都会在下面归一到同一身份。
                self.aliases.setdefault(ep.user_id, set()).add(obj.lower())
                self.self_name.setdefault(ep.user_id, obj)
                continue
            if subj.lower() in _SELF_REFS or subj.lower() in self.aliases.get(ep.user_id, ()):
                # 第一人称/用户指称，或用户声明过的任何姓名（我/用户/李雷/小雷）->
                # 归一到同一个规范 subject，使用户自己的所有 fact 共享它（否则会分散在多个
                # subject 上，档案 / 冲突消解就无法识别它们属于用户本人）。
                subj = self.self_of(ep.user_id)
            topic, topic_source = resolve_topic(
                pred, preference_topic_object(pred, obj, self.preference_object_normalization),
                item.get("topic") if self.llm_topics else None)
            topic_items.append(item)
            facts.append(
                Fact(
                    subject=subj,
                    predicate=pred,
                    object=obj,
                    # 模型输出的母语表述（中文进 -> 中文记录）；在 UI 中展示。用于嵌入/检索的
                    # 规范英文 predicate `text` 仍会照常构建。
                    display=str(item.get("text", "")).strip(),
                    user_id=ep.user_id,
                    valid_at=ep.event_time,
                    created_at=ep.ingested_at,
                    provenance=[ep.id],
                    topic=topic,
                    topic_source=topic_source,
                )
            )
        if self.llm_topics:
            log_topic_inputs(topic_items)
        return facts
