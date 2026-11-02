"""Read-only event evidence. Original documents never cross the retrieval interface."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ..types import Episode


@dataclass(frozen=True)
class EventEvidence:
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


def event_evidence(episode: Episode | EventEvidence) -> EventEvidence:
    if isinstance(episode, EventEvidence):
        return episode
    coverage = episode.summary_coverage if episode.summary else None
    return EventEvidence(
        episode.id, episode.user_id, episode.session_id, episode.role,
        episode.event_time, episode.ingested_at,
        episode.summary if episode.summary else episode.content,
        "summary" if episode.summary else "raw",
        MappingProxyType({"date": episode.metadata.get("date", "")}),
        bool(episode.summary), (bool(coverage['partial']) if coverage is not None
                                else None if episode.summary else False),
        MappingProxyType({**coverage, 'ranges': tuple(tuple(r) for r in coverage['ranges'])})
        if coverage is not None else None,
    )
