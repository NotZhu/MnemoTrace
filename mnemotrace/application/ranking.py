"""融合按相关性排列的事实候选。"""

from collections import defaultdict
from collections.abc import Iterable

from ..domain.ids import FactId


def rrf_scores(
    *rankings: Iterable[FactId], rank_constant: int
) -> list[tuple[FactId, float]]:
    """按倒数排名融合计算分数，同分按事实标识排序。"""
    scores: dict[FactId, float] = defaultdict(float)
    for ranking in rankings:
        for rank, fact_id in enumerate(ranking, start=1):
            scores[fact_id] += 1 / (rank_constant + rank)
    return sorted(scores.items(), key=lambda hit: (-hit[1], hit[0].value))
