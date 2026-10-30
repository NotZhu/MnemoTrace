"""Pure window planning over immutable source text and committed character offsets."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import groupby
from typing import Callable

from ..types import Episode

WINDOW_PREFIX = "MNEMOTRACE_WINDOW\n"


def chronology(ep: Episode):
    # Stable sorting preserves source order on tied clocks; random IDs are not a clock.
    return ep.event_time, ep.ingested_at


@dataclass(frozen=True)
class SourceSpan:
    episode: Episode
    start: int
    end: int

    @property
    def id(self) -> str:
        return f"{self.episode.id}@{self.start}:{self.end}"

    def record(self) -> dict:
        ep = self.episode
        return {"id": self.id, "episode_id": ep.id, "start": self.start, "end": self.end,
                "event_time": ep.event_time, "ingested_at": ep.ingested_at,
                "speaker": ep.speaker, "content": ep.content[self.start:self.end]}


@dataclass(frozen=True)
class ExtractionWindow:
    pending: tuple[SourceSpan, ...]
    context: tuple[SourceSpan, ...] = ()

    @property
    def scope(self) -> str:
        return self.pending[0].episode.metadata.get("memory_scope", "long")

    @property
    def content(self) -> str:
        return WINDOW_PREFIX + json.dumps(
            {"context": [s.record() for s in self.context],
             "pending": [s.record() for s in self.pending]}, ensure_ascii=False)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:24]


def _context(first: SourceSpan, history: list[Episode], budget: int) -> tuple[SourceSpan, ...]:
    ep = first.episode
    prior = []
    for e in sorted(history, key=chronology):
        if e.id == ep.id:
            break
        if (e.user_id, e.session_id) == (ep.user_id, ep.session_id) and chronology(e) <= chronology(ep):
            prior.append(e)
    candidates = [SourceSpan(e, 0, len(e.content)) for e in sorted(prior, key=chronology)]
    if first.start:
        candidates.append(SourceSpan(ep, 0, first.start))
    result = []
    for span in reversed(candidates):
        lo, hi = span.start, span.end
        # Keep a suffix without cutting a Unicode code point or exceeding the byte budget.
        while lo < hi:
            mid = (lo + hi) // 2
            if len(span.episode.content[mid:span.end].encode("utf-8")) <= budget:
                hi = mid
            else:
                lo = mid + 1
        if lo < span.end:
            result.append(SourceSpan(span.episode, lo, span.end))
            budget -= len(span.episode.content[lo:span.end].encode("utf-8"))
        if not budget:
            break
    return tuple(reversed(result))


def plan_windows(episodes: list[Episode], history: list[Episode], offset: Callable[[Episode], int],
                 measure: Callable[[ExtractionWindow], int], max_bytes: int,
                 context_bytes: int, max_windows: int | None = None) -> list[ExtractionWindow]:
    """Group only adjacent same-user/session/policy input; context never advances offsets."""
    windows = []
    key = lambda ep: (ep.user_id, ep.session_id, ep.metadata.get("memory_scope", "long"))
    for _, group in groupby(sorted(episodes, key=chronology), key):
        rows = list(group)
        index, start = 0, offset(rows[0])
        while index < len(rows):
            first = SourceSpan(rows[index], start, len(rows[index].content))
            context = _context(first, history, context_bytes)
            pending: list[SourceSpan] = []
            while index < len(rows):
                ep = rows[index]
                candidate = SourceSpan(ep, start, len(ep.content))
                if measure(ExtractionWindow(tuple([*pending, candidate]), context)) <= max_bytes:
                    pending.append(candidate)
                    index += 1
                    if index < len(rows):
                        start = offset(rows[index])
                    continue
                if pending:
                    break
                # Context is expendable; new input must always have room to make progress.
                minimal = ExtractionWindow((SourceSpan(ep, start, min(start + 1, len(ep.content))),), context)
                if context and measure(minimal) > max_bytes:
                    context = context[1:]
                    continue
                lo, hi = start, len(ep.content)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if measure(ExtractionWindow((SourceSpan(ep, start, mid),), context)) <= max_bytes:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo == start:
                    raise ValueError("extraction_max_input_bytes cannot fit prompt and source metadata")
                pending.append(SourceSpan(ep, start, lo))
                start = lo
                break
            windows.append(ExtractionWindow(tuple(pending), context))
            if max_windows is not None and len(windows) >= max_windows:
                return windows
    return windows
