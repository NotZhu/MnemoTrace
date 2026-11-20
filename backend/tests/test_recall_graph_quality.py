"""验证图召回评测的计分对照规则。"""

from importlib import import_module
from pathlib import Path

import pytest

from mnemotrace_next.domain.ids import FactId


def test_seed_ablation_keeps_ordinary_votes_and_later_graph_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """种子边仍保留普通路得分，图路中的后续边保持原名次。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    quality = import_module("recall_graph_quality")
    seed, direct, expanded = FactId("z-seed"), FactId("b-direct"), FactId("a-expanded")

    assert quality._rank_without_seed_votes(
        (seed,), (direct,), (seed, expanded), {seed}
    ) == [direct, seed, expanded]
