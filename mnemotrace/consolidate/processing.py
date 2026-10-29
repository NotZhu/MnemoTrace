"""Processing range access; the current adapter stores records with their source episode."""
from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from dataclasses import replace
from typing import Callable

from ..llm import Completion
from ..store.base import DocStore
from ..types import Episode
from .windows import ExtractionWindow

PROGRESS_KEY = "extraction_progress"


def validate_progress(content: str, metadata: dict) -> dict | None:
    if PROGRESS_KEY not in metadata:
        return None
    data = metadata[PROGRESS_KEY]
    fail = ValueError("Episode.extraction_progress: invalid processing record")
    if not isinstance(data, dict) or set(data) != {
        "content_hash", "completed_through", "facts_added", "working_added", "durable", "transient", "windows"
    }:
        raise fail
    if data["content_hash"] != hashlib.sha256(content.encode("utf-8")).hexdigest():
        raise fail
    for k in ("completed_through", "facts_added", "working_added"):
        if type(data[k]) is not int or data[k] < 0:
            raise fail
    if data["completed_through"] > len(content) or any(type(data[k]) is not bool for k in ("durable", "transient")):
        raise fail
    if not isinstance(data["windows"], list) or not data["windows"]:
        raise fail
    end = 0
    for w in data["windows"]:
        if not isinstance(w, dict) or set(w) != {"start", "end", "window_id", "finish_reason", "usage", "latency_ms"}:
            raise fail
        if (type(w["start"]) is not int or type(w["end"]) is not int or w["start"] != end
                or not (end < w["end"] <= len(content) or not content and end == w["end"] == 0)
                or not isinstance(w["window_id"], str) or not w["window_id"]
                or type(w["latency_ms"]) not in (int, float) or not math.isfinite(w["latency_ms"])
                or w["latency_ms"] < 0):
            raise fail
        Completion("", w["finish_reason"], w["usage"])
        if w["finish_reason"] not in {None, "stop", "end_turn"}:
            raise fail
        end = w["end"]
    if end != data["completed_through"]:
        raise fail
    return data


class EpisodeProcessingStore:
    """Hide document storage and metadata layout from extraction planning and application."""

    def __init__(self, documents: DocStore, write: Callable[[Episode], None]):
        self.documents, self.write = documents, write

    def episodes(self) -> list[Episode]:
        return list(self.documents.values())

    def offset(self, ep: Episode) -> int:
        from ..store import StoreFormatError
        try:
            data = validate_progress(ep.content, ep.metadata)
        except ValueError as exc:
            raise StoreFormatError(str(exc)) from exc
        return data["completed_through"] if data else 0

    def complete(self, window: ExtractionWindow, counts: dict, durable: set[str],
                 transient: set[str], completion: Completion, latency_ms: float) -> None:
        for span in window.pending:
            ep = span.episode
            previous = validate_progress(ep.content, ep.metadata)
            data = deepcopy(previous) if previous else {
                "content_hash": hashlib.sha256(ep.content.encode("utf-8")).hexdigest(),
                "completed_through": 0, "facts_added": 0, "working_added": 0,
                "durable": False, "transient": False, "windows": []}
            if data["completed_through"] != span.start:
                raise ValueError("Processing ranges must commit in source order")
            data["completed_through"] = span.end
            for name in ("facts_added", "working_added"):
                data[name] += counts[ep.id][name]
            data["durable"] |= ep.id in durable
            data["transient"] |= ep.id in transient
            data["windows"].append({"start": span.start, "end": span.end, "window_id": window.id,
                                    "finish_reason": completion.finish_reason, "usage": completion.usage,
                                    "latency_ms": latency_ms})
            updated = replace(ep, metadata=deepcopy(ep.metadata), consolidated=span.end == len(ep.content))
            updated.metadata[PROGRESS_KEY] = data
            updated.metadata.pop("extraction_error", None)
            updated.metadata["extraction_counts"] = {k: data[k] for k in ("facts_added", "working_added")}
            updated.metadata["memory_result"] = ("mixed" if data["durable"] and data["transient"] else
                                             "long" if data["durable"] else
                                             "working" if data["transient"] else "none")
            self.write(updated)
            ep.metadata, ep.consolidated = updated.metadata, updated.consolidated
