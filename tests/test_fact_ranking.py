import pytest

from mnemotrace.config import Config
from mnemotrace.retrieve.fusion import rank_facts
from mnemotrace.types import Fact


def _fact(fact_id, value):
    return Fact("Synthetic project", "tracks", value, text=f"Project tracks {value}", id=fact_id)


def test_literal_match_outranks_fused_evidence_without_borrowing_zero_score_ranks():
    literal = _fact("a", "AB-104")
    supported = _fact("b", "AB/104")
    unrelated = _fact("c", "AB-105")
    config = Config(w_sem=3, w_lex=2, w_graph=1, w_rec=1, w_sal=2, rrf_k=4)
    scores = {"c": -1.0, "a": 0.0, "b": 1.0}
    zero = dict.fromkeys(scores, 0.0)

    ranked, fused = rank_facts(
        "Find AB-104", [supported, unrelated, literal], config,
        semantic=scores, lexical=scores, graph=scores, recency=zero, salience=zero,
    )

    assert [fact.id for fact, _ in ranked] == ["a", "b", "c"]
    assert fused == pytest.approx({"a": 3 / 5, "b": 6 / 5 + 3 / 6, "c": 3 / 7})
    assert [score for _, score in ranked] == [fused["a"], fused["b"], fused["c"]]


def test_tied_signal_scores_rank_by_id_independent_of_input_order():
    low_id = _fact("a", "tea")
    high_id = _fact("b", "coffee")
    config = Config(w_sem=1, w_lex=2, w_graph=3, w_rec=4, w_sal=5, rrf_k=9)

    for facts in ([low_id, high_id], [high_id, low_id]):
        for tied in ({"a": 1.0, "b": 1.0}, {"b": 1.0, "a": 1.0}):
            ranked, fused = rank_facts(
                "beverages", facts, config,
                semantic=tied, lexical=tied, graph=tied, recency=tied, salience=tied,
            )
            assert [fact.id for fact, _ in ranked] == ["a", "b"]
            assert fused == pytest.approx({"a": 15 / 10, "b": 15 / 11})


def test_tied_fused_scores_rank_by_id():
    low_id = _fact("a", "tea")
    high_id = _fact("b", "coffee")
    config = Config(w_sem=1, w_lex=1, w_graph=0, w_rec=0, w_sal=0, rrf_k=4)
    zero = {"b": 0.0, "a": 0.0}

    ranked, fused = rank_facts(
        "beverages", [high_id, low_id], config,
        semantic={"b": 1.0}, lexical={"a": 1.0}, graph={}, recency=zero, salience=zero,
    )

    assert [fact.id for fact, _ in ranked] == ["a", "b"]
    assert fused == pytest.approx({"a": 1 / 5, "b": 1 / 5})


def test_empty_fact_ranking():
    assert rank_facts(
        "beverages", [], Config(), semantic={}, lexical={}, graph={}, recency={}, salience={},
    ) == ([], {})
