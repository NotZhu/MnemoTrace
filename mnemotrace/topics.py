"""事实主题契约；不负责分类、画像分组或敏感性判断。"""
from __future__ import annotations

from typing import Literal, get_args

Topic = Literal["food", "travel", "music", "film_tv", "sports", "reading", "hobbies",
                "environment", "health", "work", "learning", "technology", "finance",
                "relationships", "unknown"]
TopicSource = Literal["unset", "rule", "llm", "user"]
TOPICS = get_args(Topic)
TOPIC_SOURCES = get_args(TopicSource)


def validate_topic(topic: str, source: str) -> None:
    """验证恢复与构造使用的同一契约，避免来源与主题互相矛盾。"""
    if not isinstance(topic, str) or topic not in TOPICS:
        raise ValueError("topic must be one of " + ", ".join(TOPICS))
    if not isinstance(source, str) or source not in TOPIC_SOURCES:
        raise ValueError("topic_source must be one of " + ", ".join(TOPIC_SOURCES))
    if source == "unset" and topic != "unknown":
        raise ValueError("topic_source unset requires topic unknown")
    if source in {"rule", "llm"} and topic == "unknown":
        raise ValueError("topic_source rule/llm requires a known topic")
