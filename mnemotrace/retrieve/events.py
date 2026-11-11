"""Read-only source digest evidence."""
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


def digest_evidence(*, id, user_id, session_id, role, event_time, ingested_at, digest, date, coverage):
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
    return digest_evidence(id=episode.id, user_id=episode.user_id, session_id=episode.session_id,
                           role=episode.role, event_time=episode.event_time, ingested_at=episode.ingested_at,
                           digest=episode.digest, date=episode.metadata.get("date", ""),
                           coverage=episode.digest_coverage)
