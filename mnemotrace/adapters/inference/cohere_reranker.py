"""从 Cohere v2 兼容端点读取完整候选池的相关性分数。"""

import asyncio
from collections.abc import Sequence
from http import HTTPStatus

from ...domain.ids import FactId
from ._http import (
    InferenceHTTP,
    InferenceRequestError,
    RequestBudget,
    require_response_object,
)


class CohereReranker:
    """使用部署时装配的端点和模型对候选事实打分。"""

    def __init__(self, http: InferenceHTTP, *, model: str, timeout: float) -> None:
        self._http = http
        self._model = model
        self._timeout = timeout

    async def rerank(
        self, query: str, documents: Sequence[tuple[FactId, str]]
    ) -> Sequence[tuple[FactId, float]]:
        """返回上游为每条候选给出的相关性分数。"""
        if not documents:
            return ()
        deadline = asyncio.get_running_loop().time() + self._timeout
        try:
            async with asyncio.timeout_at(deadline):
                body = await self._http.post(
                    "rerank",
                    {
                        "model": self._model,
                        "query": query,
                        "documents": [text for _, text in documents],
                        "top_n": len(documents),
                    },
                    budget=RequestBudget(deadline),
                    mode="rerank",
                )
        except InferenceRequestError as exc:
            if exc.status_code == HTTPStatus.GATEWAY_TIMEOUT:
                raise TimeoutError("重排上游网关超时") from exc
            raise
        results = body.get("results")
        if not isinstance(results, list) or len(results) != len(documents):
            raise InferenceRequestError("protocol")
        scores: dict[int, float] = {}
        for item in results:
            record = require_response_object(item)
            index, score = record.get("index"), record.get("relevance_score")
            if (
                type(index) is not int
                or not 0 <= index < len(documents)
                or index in scores
            ):
                raise InferenceRequestError("protocol")
            if (
                isinstance(score, bool)
                or not isinstance(score, (float, int))
                or not 0 <= score <= 1
            ):
                raise InferenceRequestError("protocol")
            scores[index] = float(score)
        return tuple((documents[index][0], score) for index, score in scores.items())
