"""定义候选事实的相关性重排端口。"""

from collections.abc import Sequence
from typing import Protocol

from ..domain.ids import FactId


class Reranker(Protocol):
    """按查询与候选正文的相关性重排事实。"""

    async def rerank(
        self, query: str, documents: Sequence[tuple[FactId, str]]
    ) -> Sequence[tuple[FactId, float]]:
        """为候选事实逐条计算相关性分数。

        Args:
            query: 待匹配的查询文本。
            documents: 已授权的候选事实标识及正文。

        Returns:
            每条候选恰好一个标识与有限分数对。分数在 [0, 1] 内，顺序任意。

        Raises:
            InferenceError: 请求失败或上游响应无法解析。
            TimeoutError: 重排超过实现配置的时限。
        """
        ...
