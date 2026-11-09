"""写入时的主题判定与模型主题协议；不读取画像、不改变事实或敏感性。"""
from __future__ import annotations

import logging

from ..topics import TOPICS, Topic, TopicSource

_LOG = logging.getLogger(__name__)
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


def topic_system(system: str) -> str:
    return system + _TOPIC_PROTOCOL


def log_topic_inputs(items: list[dict]) -> None:
    """单次属性或结论抽取调用内聚合，非法主题至多一条 WARNING。

    不跨独立模型调用累计；一个窗口可包含多个 episode。只记录数量，不带入模型字段、正文或用户标识。
    """
    invalid = sum("topic" in item and (not isinstance(item["topic"], str) or item["topic"] not in TOPICS)
                  for item in items)
    if invalid:
        _LOG.warning("LLM topic invalid output: invalid=%d", invalid)
    _LOG.debug("LLM topic inputs: missing=%d, abstained=%d",
               sum("topic" not in item for item in items),
               sum(item.get("topic") == "unknown" for item in items))


def resolve_topic(value: object) -> tuple[Topic, TopicSource]:
    if isinstance(value, str) and value in TOPICS and value != "unknown":
        return value, "llm"
    return "unknown", "unset"
