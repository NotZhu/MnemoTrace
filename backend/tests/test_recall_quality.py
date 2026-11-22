"""验证新近度质量对照的查询向量复用。"""

import asyncio
from collections import Counter
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path

import pytest

from mnemotrace_next.ports.inference import Embedder
from tests.ports.fakes import FakeStorage


def test_recency_quality_generates_each_query_vector_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选探测和两次公开召回只需向上游取一次查询向量。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    quality = import_module("recall_quality")
    embedder: Embedder = quality._RecencyEmbedder()
    original_embed = embedder.embed
    calls: Counter[str] = Counter()

    async def count_queries(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if len(texts) == 1:
            calls[texts[0]] += 1
        return await original_embed(texts)

    monkeypatch.setattr(embedder, "embed", count_queries)
    report = asyncio.run(quality._run_recency_suite(FakeStorage(), embedder))

    assert report["passed"] is True
    assert calls == Counter(case.text for case in quality.RECENCY_DATASET.queries)
