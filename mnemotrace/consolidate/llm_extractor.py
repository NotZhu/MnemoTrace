"""LLM 驱动的 fact 提取器。与离线 RuleExtractor 相同的 `.extract(episode) -> list[Fact]` API，
因此整条管线（图构建、冲突消解、检索）都不变——只是提取质量提升。给 Memory 传入 `llm` 时自动启用。"""
from __future__ import annotations

import json
import re

from ..llm import LLM
from ..types import Episode, Fact

EXTRACT_SYSTEM = (
    "你是长期记忆系统中的精确信息提取引擎。\n"
    "从多轮对话中，提取明确陈述的、独立且具有长期价值的事实，涉及用户及其提到的人物或事物，"
    "包括身份、属性、偏好、关系、所有物、目标、计划，以及事件和发生时间。\n"
    '仅输出 JSON 对象数组，每个对象包含 "subject"、"predicate"、"object"、"text" 四个字段。'
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
    "不要推断或编造对话中未明确陈述的事实。如果没有具有长期价值的事实，输出 []。"
)

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


def _complete_objects_prefix(text: str) -> str:
    """用已完整结束的 object 重建合法 JSON 数组，丢弃被截断的尾巴。"""
    if not text.startswith("["):
        return ""
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(text[start:i + 1])
                start = -1
    return "[" + ",".join(objects) + "]" if objects else ""


def parse_json_facts(raw: str) -> list[dict]:
    """宽容的 JSON 数组解析：处理代码围栏和前后杂文。"""
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    candidate = text[start : end + 1] if (start != -1 and end > start) else text
    # 被 max_tokens 截断的回复会留下合法前缀和写了一半的最后一个 object。抢救完整 object 严格优于
    # 丢弃整批——否则长会话正是如此：模型用满预算，提取出的所有条目全部作废。
    salvaged = _complete_objects_prefix(candidate)
    for attempt in (candidate, text, salvaged):
        if not attempt:
            continue
        try:
            data = json.loads(attempt)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            continue
    return []


class LLMExtractor:
    def __init__(self, llm: LLM, system: str = EXTRACT_SYSTEM, template: str = EXTRACT_TEMPLATE) -> None:
        self.llm = llm
        self.system = system
        self.template = template
        # 可选的按用户指令，附加在 system prompt 末尾：用户想记录（或不想记录）什么。
        # 由 Memory.policy["extract_instruction"] 设置——即 console 里那个核心的"要记录什么记忆"开关。
        self.instruction = ""
        self.self_name: dict[str, str] = {}        # user_id -> 规范名（最先声明的那个）
        self.aliases: dict[str, set] = {}          # user_id -> {所有声明的姓名/昵称}（共指消解）

    def self_of(self, user_id: str) -> str:
        return self.self_name.get(user_id, user_id)

    def _effective_system(self) -> str:
        if self.instruction.strip():
            return (self.system + "\n\n以下是用户关于记录内容的补充要求，请遵守，但仍只输出上述 JSON 数组：\n"
                    + self.instruction.strip())
        return self.system

    def extract(self, ep: Episode) -> list[Fact]:
        return self.facts_from(ep, self.raw_items(ep))

    def raw_items(self, ep: Episode) -> list[dict]:
        """只做模型调用——不触碰任何共享状态，因此可以安全并发。

        从 `facts_from` 中拆出来，是因为身份注册（哪个名字是规范名）依赖顺序：放在线程池里跑时它
        按完成顺序解析，导致同一 episode 这次产出 "Evan lives in X"、下次产出 "user lives in X"。
        fact 集随之改变，persona prompt 随之改变，固定的提取缓存也随之失效。现在调用方并行发起
        模型调用，而有状态的转换按时间顺序执行。
        """
        return parse_json_facts(
            self.llm.complete(self.template.format(speaker=ep.speaker, content=ep.content),
                              system=self._effective_system())
        )

    def facts_from(self, ep: Episode, items: list[dict]) -> list[Fact]:
        """把解析出的条目转成 Fact，并注册姓名/别名。有意依赖顺序：请按时间顺序调用，
        使规范名是用户实际最先说出的那个。"""
        facts: list[Fact] = []
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
                subj = self.self_of(ep.user_id) if self.self_of(ep.user_id) != ep.user_id else subj
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
                )
            )
        return facts
