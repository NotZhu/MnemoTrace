"""验证倒数排名融合的计分和稳定顺序。"""

from mnemotrace.application.ranking import rrf_scores
from mnemotrace.domain.ids import FactId


def test_rrf_combines_rankings_and_breaks_ties_by_fact_id() -> None:
    winner, first, second = FactId("winner"), FactId("a"), FactId("b")
    tied_score = 1 / 6 + 1 / 7
    assert rrf_scores(
        (winner, second, first), (), (winner, first, second), rank_constant=4
    ) == [(winner, 2 / 5), (first, tied_score), (second, tied_score)]
    assert rrf_scores((), (), rank_constant=4) == []
