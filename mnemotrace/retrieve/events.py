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
    speaker: str
    event_time: float
    ingested_at: float
    text: str
    representation: str
    metadata: Mapping[str, str]


def event_evidence(episode: Episode | EventEvidence) -> EventEvidence:
    if isinstance(episode, EventEvidence):
        return episode
    return EventEvidence(
        episode.id, episode.user_id, episode.session_id, episode.speaker,
        episode.event_time, episode.ingested_at,
        episode.summary if episode.summary else episode.content,
        "summary" if episode.summary else "raw",
        MappingProxyType({"date": episode.metadata.get("date", "")}),
    )
