"""Read-only source digest evidence."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ..types import Episode


@dataclass(frozen=True)
class EventEvidence:
    """供检索读取的来源证据及其时间、出处和覆盖元数据。"""
    id: str
    user_id: str
    session_id: str
    role: str
    event_time: float | None
    ingested_at: float
    text: str
    representation: str
    metadata: Mapping[str, str]
    derived: bool = False
    partial: bool | None = False
    coverage: Mapping | None = None


def digest_evidence(*, id, user_id, session_id, role, event_time, ingested_at, digest, date, coverage):
    """将来源摘要转换为只读证据。

    Args:
        id: 原文记录 ID。
        user_id: 原文所属的用户 ID。
        session_id: 原文所属的会话 ID。
        role: 消息发送者的角色。
        event_time: 可选的事件时间戳。
        ingested_at: 原文写入时间戳。
        digest: 来源摘要文本。
        date: 展示用日期。
        coverage: 可选的摘要覆盖信息。

    Returns:
        来源摘要及其元数据。

    Raises:
        ValueError: 摘要为空。
    """
    if not digest:
        raise ValueError("Expected a non-empty source digest")
    return EventEvidence(
        id, user_id, session_id, role, event_time, ingested_at, digest, "digest",
        MappingProxyType({"date": date}), True,
        bool(coverage['partial']) if coverage is not None else None,
        MappingProxyType({**coverage, 'ranges': tuple(tuple(r) for r in coverage['ranges'])})
        if coverage is not None else None,
    )


def event_evidence(episode: Episode) -> EventEvidence:
    """从离线原文事件读取其摘要证据。

    Args:
        episode: 带已生成摘要的原文事件。

    Returns:
        来源摘要及其元数据。
    """
    return digest_evidence(id=episode.id, user_id=episode.user_id, session_id=episode.session_id,
                           role=episode.role, event_time=episode.event_time, ingested_at=episode.ingested_at,
                           digest=episode.digest, date=episode.metadata.get("date", ""),
                           coverage=episode.digest_coverage)
