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
        """写入一条事件（episode）。

        Args:
            content: 事件内容。
            user_id: 用户 ID。
            session_id: 会话 ID。
            role: 说话者。
            event_time: 事件发生时间（世界时间）。
            extract: 是否请求后续事实抽取。
        Returns:
            写入的 Episode 对象。
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
        """返回等待事实抽取的 episode。"""
        return [ep for ep in self.episodes_doc.values() if ep.extract and not ep.consolidated]
