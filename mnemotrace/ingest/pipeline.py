"""System-1 事件写入：保存原始内容及归属信息。

事件标记为待整合；整合由调用方触发，可采用同步或异步方式。
"""
from __future__ import annotations

from typing import Optional

from ..store import DocStore
from ..types import Episode


class Ingestor:
    """事件写入器。"""
    def __init__(self, episodes_doc: DocStore) -> None:
        """绑定保存原文事件的文档存储。

        Args:
            episodes_doc: 原文事件的存储后端。
        """
        self.episodes_doc = episodes_doc

    def ingest(
        self,
        content: str,
        user_id: str = "default",
        session_id: str = "default",
        role: str = "user",
        event_time: Optional[float] = None,
        *,
        extract: bool = True,
    ) -> Episode:
        """保存一条原始事件。

        Args:
            content: 事件原文。
            user_id: 用户 ID。
            session_id: 会话 ID。
            role: 消息发送者的角色。
            event_time: 事件时间戳。省略时表示未知。
            extract: 是否标记为待抽取。

        Returns:
            已保存的 Episode。

        Raises:
            ValueError: extract 不是布尔值。
        """
        ep = Episode(
            content=content,
            user_id=user_id,
            session_id=session_id,
            role=role,
            event_time=event_time,
            extract=extract,
        )
        self.episodes_doc.put(ep.id, ep)
        return ep

    def pending(self) -> list[Episode]:
        """列出请求抽取且尚未整合的原文事件。"""
        return [ep for ep in self.episodes_doc.values() if ep.extract and not ep.consolidated]
