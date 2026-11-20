"""从已授权空间的同一快照召回事实证据。"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ..domain._validation import optional_timestamp
from ..domain.errors import DomainError
from ..domain.ids import FactId, MessageId, SpaceId
from ..ports.inference import Embedder, InferenceError
from ..ports.queries import FactFilter
from ..ports.reranker import Reranker
from ..ports.runtime import Clock
from ..ports.storage import Storage

DEFAULT_TOP_K = 10
_MAX_TOP_K = 100
_MAX_QUERY_CHARS = 4096
_RRF_K = 60
_VECTOR_MIN_COSINE = 0.5

type _DegradedReason = Literal["embedding_timeout", "reranker_timeout"]


@dataclass(frozen=True)
class RecallEvidence:
    """保存可见事实及其有效区间和来源标识。"""

    fact_id: FactId
    text: str
    valid_at: float | None
    invalid_at: float | None
    source_message_ids: tuple[MessageId, ...]


@dataclass(frozen=True)
class RecallResult:
    """保存按相关性排列的证据及可选的超时降级原因。"""

    evidence: tuple[RecallEvidence, ...]
    degraded_reason: _DegradedReason | None = None


class Recall:
    """独立装配的只读召回用例。

    Args:
        space_id: 服务端选定的空间，存储会话仍会校验访问权。
        storage: 已绑定认证账号的存储端口。
        embedder: 与事实向量使用相同嵌入空间的查询端口。
        clock: 提供 UTC epoch 秒的时钟。
        exclude_sensitive: 由服务端设置，受限调用为真。
        reranker: 由服务端装配且获准接收候选正文的可选重排端口。

    Raises:
        ValueError: 缺少查询嵌入模型或可见性选项无效。
    """

    def __init__(
        self,
        *,
        space_id: SpaceId,
        storage: Storage,
        embedder: Embedder,
        clock: Clock,
        exclude_sensitive: bool = False,
        reranker: Reranker | None = None,
    ) -> None:
        if embedder is None:
            raise ValueError("Memory 需要配置嵌入模型")
        if type(exclude_sensitive) is not bool:
            raise ValueError("exclude_sensitive 须为布尔值")
        self._space_id = space_id
        self._storage = storage
        self._embedder = embedder
        self._clock = clock
        self._exclude_sensitive = exclude_sensitive
        self._reranker = reranker

    async def recall(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        as_of: float | None = None,
        rerank: bool = False,
    ) -> RecallResult:
        """按查询相关性召回指定时点有效的可见事实。

        Args:
            query: 非空查询，最长 4096 个字符。
            top_k: 最多返回的证据条数，取 1 至 100。
            as_of: 显式有效时点，以 UTC epoch 秒表示。省略时查当前事实。
            rerank: 是否调用已装配的重排器调整候选顺序。

        Returns:
            按相关性排列的事实证据及可选的超时降级原因。

        Raises:
            ValueError: 输入无效，或显式开启重排但未装配重排器。
            LookupError: 无权访问绑定空间。
            InferenceError: 推理端口请求失败或返回无效结果。
        """
        query = _validate_query(query)
        if type(top_k) is not int or not 1 <= top_k <= _MAX_TOP_K:
            raise ValueError(f"top_k 须为 1 至 {_MAX_TOP_K} 的整数")
        if type(rerank) is not bool:
            raise ValueError("rerank 须为布尔值")
        reranker = self._reranker if rerank else None
        if rerank and reranker is None:
            raise ValueError("开启重排需要装配重排器")
        filters = FactFilter(
            valid_at=optional_timestamp(as_of, "as_of", DomainError),
            exclude_sensitive=self._exclude_sensitive,
        )
        reference_time = self._clock.now()
        per_channel_limit = _candidate_limit(top_k)

        # 进入会话已经完成授权。先退出事务，再把查询发给嵌入端点。
        async with self._storage.session(self._space_id):
            pass
        vector: Sequence[float] | None
        degraded_reason: _DegradedReason | None = None
        try:
            vectors = await self._embedder.embed((query,))
        except TimeoutError:
            vector = None
            reranker = None
            degraded_reason = "embedding_timeout"
        else:
            if not isinstance(vectors, Sequence) or len(vectors) != 1:
                raise InferenceError("查询嵌入须返回一条向量")
            vector = vectors[0]
            if vector is None or len(vector) == 0:
                raise InferenceError("查询嵌入返回了空向量")

        fact_load_limit = top_k if reranker is None else min(100, max(20, 2 * top_k))
        async with self._storage.session(self._space_id) as reader:
            lexical = await reader.search_lexical(
                query, per_channel_limit, filters=filters, reference_time=reference_time
            )
            semantic: Sequence[tuple[FactId, float]] = ()
            if vector is not None:
                semantic = await reader.search_vectors(
                    vector,
                    per_channel_limit,
                    filters=filters,
                    reference_time=reference_time,
                    embedding_space=self._embedder.embedding_space,
                    min_score=_VECTOR_MIN_COSINE,
                )
            ids = _fuse_rankings(lexical, semantic)[:fact_load_limit]
            if not ids:
                return RecallResult((), degraded_reason)
            facts = await reader.get_facts(
                ids, exclude_sensitive=self._exclude_sensitive
            )

        if reranker is not None and len(ids) > 1:
            documents = tuple((fact_id, facts[fact_id].text) for fact_id in ids)
            try:
                ranked = await reranker.rerank(query, documents, top_k)
            except TimeoutError:
                degraded_reason = "reranker_timeout"
            else:
                ids = _fill_reranking(ids, ranked, top_k)

        evidence = []
        for fact_id in ids[:top_k]:
            fact = facts[fact_id]
            evidence.append(
                RecallEvidence(
                    fact.id, fact.text, fact.valid_at, fact.invalid_at, fact.provenance
                )
            )
        return RecallResult(tuple(evidence), degraded_reason)


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS:
        raise ValueError(f"查询须为最多 {_MAX_QUERY_CHARS} 个字符的字符串")
    query = query.strip()
    if not query or "\x00" in query:
        raise ValueError("查询须为非空且不含空字符的字符串")
    try:
        query.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("查询须能编码为 UTF-8") from exc
    return query


def _candidate_limit(top_k: int) -> int:
    return min(200, max(32, 4 * top_k))


def _fuse_rankings(
    *rankings: Sequence[tuple[FactId, float]],
) -> list[FactId]:
    scores: dict[FactId, float] = defaultdict(float)
    for ranking in rankings:
        for rank, (fact_id, _) in enumerate(ranking, start=1):
            scores[fact_id] += 1 / (_RRF_K + rank)
    return sorted(scores, key=lambda fact_id: (-scores[fact_id], fact_id.value))


def _fill_reranking(
    candidates: Sequence[FactId], ranked: Sequence[FactId], limit: int
) -> list[FactId]:
    if (
        not isinstance(ranked, Sequence)
        or isinstance(ranked, (str, bytes, bytearray))
        or len(ranked) > limit
    ):
        raise InferenceError("重排返回的标识序列无效或超过上限")
    allowed = set(candidates)
    seen: set[FactId] = set()
    for fact_id in ranked:
        if not isinstance(fact_id, FactId) or fact_id not in allowed or fact_id in seen:
            raise InferenceError("重排返回了重复或不属于本次候选的事实标识")
        seen.add(fact_id)
    return list(ranked) + [fact_id for fact_id in candidates if fact_id not in seen]
