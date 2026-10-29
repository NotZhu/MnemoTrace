"""System-1 事件写入：保存原始内容及归属信息，并生成或接收文本向量。

事件标记为待整合；整合由调用方触发，可采用同步或异步方式。
"""
from __future__ import annotations

from typing import Optional

from ..embed import Embedder
from ..store import DocStore, VectorStore
from ..types import Episode
from ..util import now


class Ingestor:
    """事件写入器。"""
    def __init__(
        self,
        episodes_doc: DocStore,
        episodes_vec: VectorStore,
        embedder: Embedder,
    ) -> None:
        """初始化事件写入器。

        Args:
            episodes_doc: 事件文档存储。
            episodes_vec: 事件向量存储。
            embedder: 文本嵌入器。
        """
        self.episodes_doc = episodes_doc
        self.episodes_vec = episodes_vec
        self.embedder = embedder

    def ingest(
        self,
        content: str,
        user_id: str = "default",
        session_id: str = "default",
        speaker: str = "user",
        event_time: Optional[float] = None,
        embedding: Optional[list] = None,
    ) -> Episode:
        """写入一条事件（episode）。

        Args:
            content: 事件内容。
            user_id: 用户 ID。
            session_id: 会话 ID。
            speaker: 说话者。
            event_time: 事件发生时间（世界时间）。
            embedding: 事件内容的预计算嵌入向量。
        Returns:
            写入的 Episode 对象。
        """
        ep = Episode(
            content=content,
            user_id=user_id,
            session_id=session_id,
            speaker=speaker,
            event_time=event_time if event_time is not None else now(),
        )
        # 优先使用预计算的向量；未提供时由嵌入器处理原文。
        ep.embedding = embedding if embedding is not None else self.embedder.embed(content)
        # 新事件默认为待整合，后续由调用方触发 System-2 处理。
        self.episodes_doc.put(ep.id, ep)  # 保存原始事件
        self.episodes_vec.upsert(ep.id, ep.embedding, ep)  # 保存向量及关联事件，供检索
        return ep

    def pending(self) -> list[Episode]:
        """返回尚未整合的 episode 列表。"""
        return [ep for ep in self.episodes_doc.values() if not ep.consolidated]
