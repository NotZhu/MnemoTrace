"""定义候选事实的相关性重排端口。"""

from collections.abc import Sequence
from typing import Protocol

from ..domain.ids import FactId


class Reranker(Protocol):
    """按查询与候选正文的相关性重排事实。"""

    async def rerank(
        self, query: str, documents: Sequence[tuple[FactId, str]], limit: int
    ) -> Sequence[FactId]:
        """从给定候选中选出最相关的事实。

        Args:
            query: 待匹配的查询文本。
            documents: 已授权的候选事实标识及正文。
            limit: 最大返回条数，须为正整数。

        Returns:
            至多 limit 个来自输入且互异的标识，按相关性从高到低排列。
            空输入对应空序列。
        """
        ...
