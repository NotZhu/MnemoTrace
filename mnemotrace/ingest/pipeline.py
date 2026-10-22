"""System-1：热写入路径。关键路径上没有 LLM —— 追加无损 episode、标记归属、计算轻量嵌入，
然后交给异步整合。目标：感知延迟 50ms 以内。"""
from __future__ import annotations

from typing import Optional

from ..embed import Embedder
from ..store import DocStore, VectorStore
from ..types import Episode
from ..util import now


class Ingestor:
    def __init__(
        self,
        episodes_doc: DocStore,
        episodes_vec: VectorStore,
        embedder: Embedder,
    ) -> None:
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
        ep = Episode(
            content=content,
            user_id=user_id,
            session_id=session_id,
            speaker=speaker,
            event_time=event_time if event_time is not None else now(),
        )
        # 接受预计算的 embedding，调用方就能在一次 model.encode 里批量嵌入多个 episode，
        # 比逐条 embed 快 10 倍以上（一次性写入整段会话 haystack 时尤为明显）。
        ep.embedding = embedding if embedding is not None else self.embedder.embed(content)
        # 只追加日志 + episodic 向量索引；consolidated=False 意味着进入 System-2 的处理队列。
        self.episodes_doc.put(ep.id, ep)
        self.episodes_vec.upsert(ep.id, ep.embedding, ep)
        return ep

    def pending(self) -> list[Episode]:
        """尚未整合的 episode（即 System-2 的工作队列）。"""
        return [ep for ep in self.episodes_doc.values() if not ep.consolidated]
