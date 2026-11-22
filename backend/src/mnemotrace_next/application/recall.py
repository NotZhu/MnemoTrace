"""从已授权空间的同一快照召回事实证据。"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import exp2, isfinite
from numbers import Real
from typing import Literal

from ..domain._validation import optional_timestamp
from ..domain.errors import DomainError
from ..domain.fact import Fact
from ..domain.ids import EntityId, FactId, MessageId, SpaceId
from ..domain.values import EntityRef, Predicate
from ..ports.inference import Embedder, InferenceError
from ..ports.queries import FactFilter
from ..ports.reranker import Reranker
from ..ports.runtime import Clock
from ..ports.storage import Storage
from .graph_recall import GraphRecallOptions, graph_ranking

DEFAULT_TOP_K = 10
MAX_TOP_K = 100
MAX_QUERY_CHARS = 4096
_RRF_K = 60
_VECTOR_MIN_COSINE = 0.5
_MAX_RANKING_BOOST = 0.05
_RECENCY_HALF_LIFE_SECONDS = 30 * 86_400

type RecallDegradedReason = Literal["embedding_timeout", "reranker_timeout"]


@dataclass(frozen=True)
class RecallRelation:
    """保存实体关系事实的两个端点及规范谓词。"""

    subject_id: EntityId
    predicate: Predicate
    object_entity_id: EntityId


@dataclass(frozen=True)
class RecallEvidence:
    """保存可见事实及其有效区间和来源标识。"""

    fact_id: FactId
    text: str
    valid_at: float | None
    invalid_at: float | None
    source_message_ids: tuple[MessageId, ...]
    relation: RecallRelation | None = None

    @classmethod
    def from_fact(cls, fact: Fact) -> "RecallEvidence":
        """从领域事实投影召回证据。"""
        relation = (
            RecallRelation(
                fact.subject.entity_id, fact.predicate, fact.object.entity_id
            )
            if isinstance(fact.object, EntityRef)
            else None
        )
        return cls(
            fact.id,
            fact.text,
            fact.valid_at,
            fact.invalid_at,
            fact.provenance,
            relation,
        )


@dataclass(frozen=True)
class RecallResult:
    """保存按相关性排列的证据及可选的超时降级原因。"""

    evidence: tuple[RecallEvidence, ...]
    degraded_reason: RecallDegradedReason | None = None


@dataclass(frozen=True)
class RecallUsage:
    """保存最终证据及后台复核所需的原读快照信息。"""

    space_id: SpaceId
    fact_ids: tuple[FactId, ...]
    reference_time: float
    erasure_token: str
    exclude_sensitive: bool


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
        graph: GraphRecallOptions | None = None,
        prefer_recent: bool = False,
    ) -> RecallResult:
        """按查询相关性召回指定时点有效的可见事实。

        Args:
            query: 非空查询，最长 4096 个字符。
            top_k: 最多返回的证据条数，取 1 至 100。
            as_of: 显式有效时点，以 UTC epoch 秒表示。省略时查当前事实。
            rerank: 是否使用已装配的重排器计算候选相关性。
            graph: 图召回设置，省略时使用普通召回。
            prefer_recent: 是否在当前事实中小幅优先近期生效的候选。

        Returns:
            按相关性排列的事实证据及可选的超时降级原因。

        Raises:
            ValueError: 输入无效，或显式开启重排但未装配重排器。
            LookupError: 无权访问绑定空间。
            InferenceError: 推理端口请求失败或返回无效结果。
        """
        result, _ = await self.recall_with_usage(
            query,
            track_usage=False,
            top_k=top_k,
            as_of=as_of,
            rerank=rerank,
            graph=graph,
            prefer_recent=prefer_recent,
        )
        return result

    async def recall_with_usage(
        self,
        query: str,
        *,
        track_usage: bool,
        top_k: int = DEFAULT_TOP_K,
        as_of: float | None = None,
        rerank: bool = False,
        graph: GraphRecallOptions | None = None,
        prefer_recent: bool = False,
    ) -> tuple[RecallResult, RecallUsage | None]:
        """执行只读召回，按服务端策略返回可异步强化的证据凭据。"""
        query = _validate_query(query)
        if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k 须为 1 至 {MAX_TOP_K} 的整数")
        if type(rerank) is not bool:
            raise ValueError("rerank 须为布尔值")
        if type(prefer_recent) is not bool:
            raise ValueError("prefer_recent 须为布尔值")
        if graph is not None and not isinstance(graph, GraphRecallOptions):
            raise ValueError("graph 须为 GraphRecallOptions 或 None")
        reranker = self._reranker if rerank else None
        if rerank and reranker is None:
            raise ValueError("开启重排需要装配重排器")
        filters = FactFilter(
            valid_at=optional_timestamp(as_of, "as_of", DomainError),
            exclude_sensitive=self._exclude_sensitive,
        )
        reference_time = self._clock.now()
        use_recency = prefer_recent and filters.valid_at is None
        per_channel_limit = _candidate_limit(top_k)

        # 进入会话已经完成授权。先退出事务，再把查询发给嵌入端点。
        async with self._storage.session(self._space_id):
            pass
        vector: Sequence[float] | None
        degraded_reason: RecallDegradedReason | None = None
        try:
            vectors = await self._embedder.embed((query,))
        except TimeoutError:
            vector = None
            reranker = None
            degraded_reason = "embedding_timeout"
        else:
            vector = _query_vector(vectors, self._embedder.dim)

        fact_load_limit = top_k if reranker is None else min(100, max(20, 2 * top_k))
        erasure_token = None
        async with self._storage.session(self._space_id) as reader:
            use_salience = (
                filters.valid_at is None and (await reader.get_space()).memory_decay
            )
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
            lexical_ids = tuple(fact_id for fact_id, _ in lexical)
            semantic_ids = tuple(fact_id for fact_id, _ in semantic)
            graph_ids = (
                await graph_ranking(
                    reader,
                    graph,
                    lexical_ids,
                    semantic_ids,
                    filters=filters,
                    reference_time=reference_time,
                    top_k=top_k,
                )
                if graph is not None
                else ()
            )
            ranking = _rrf_scores(lexical_ids, semantic_ids, graph_ids)
            candidates = (
                _candidates_within_boost(ranking, fact_load_limit)
                if reranker is None and (use_recency or use_salience)
                else ranking[:fact_load_limit]
            )
            ids = [fact_id for fact_id, _ in candidates]
            if not ids:
                return RecallResult((), degraded_reason), None
            facts = await reader.get_facts(
                ids, exclude_sensitive=self._exclude_sensitive
            )
            salience = (
                await reader.get_salience(
                    ids, exclude_sensitive=self._exclude_sensitive
                )
                if use_salience
                else {}
            )
            if use_salience and track_usage and degraded_reason is None:
                erasure_token = await reader.get_erasure_token()

        scores = dict(candidates)
        if reranker is not None and len(ids) > 1:
            documents = tuple((fact_id, facts[fact_id].text) for fact_id in ids)
            try:
                ranked = await reranker.rerank(query, documents)
            except TimeoutError:
                degraded_reason = "reranker_timeout"
            else:
                scores = _relevance_scores(ids, ranked)

        if reranker is not None or use_recency or use_salience:

            def ranking_key(fact_id: FactId) -> tuple[float, float]:
                score = scores[fact_id]
                return (
                    _score_with_boosts(
                        score,
                        _recency_bonus(facts[fact_id].valid_at, reference_time)
                        if use_recency
                        else 0.0,
                        salience[fact_id].normalized_at(reference_time)
                        if use_salience
                        else 0.0,
                    ),
                    score,
                )

            ids.sort(key=ranking_key, reverse=True)

        final_ids = tuple(ids[:top_k])
        evidence = tuple(
            RecallEvidence.from_fact(facts[fact_id]) for fact_id in final_ids
        )
        usage = (
            RecallUsage(
                self._space_id,
                final_ids,
                reference_time,
                erasure_token,
                self._exclude_sensitive,
            )
            if erasure_token is not None and degraded_reason is None
            else None
        )
        return RecallResult(evidence, degraded_reason), usage


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"查询须为最多 {MAX_QUERY_CHARS} 个字符的字符串")
    query = query.strip()
    if not query or "\x00" in query:
        raise ValueError("查询须为非空且不含空字符的字符串")
    try:
        query.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("查询须能编码为 UTF-8") from exc
    return query


def _query_vector(vectors: object, dimension: int) -> tuple[float, ...]:
    if (
        not isinstance(vectors, Sequence)
        or isinstance(vectors, (str, bytes, bytearray))
        or len(vectors) != 1
    ):
        raise InferenceError("查询嵌入须返回一条向量")
    vector = vectors[0]
    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes, bytearray)):
        raise InferenceError("查询嵌入须返回数值序列")
    if not vector or len(vector) != dimension:
        raise InferenceError("查询嵌入向量维度与 Embedder 配置不一致")

    components: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise InferenceError("查询嵌入向量分量须为有限实数")
        try:
            component = float(value)
        except OverflowError:
            raise InferenceError("查询嵌入向量分量须为有限实数") from None
        if not isfinite(component):
            raise InferenceError("查询嵌入向量分量须为有限实数")
        components.append(component)
    if not any(components):
        raise InferenceError("查询嵌入向量不能全为零")
    return tuple(components)


def _candidate_limit(top_k: int) -> int:
    return min(200, max(32, 4 * top_k))


def _fuse_rankings(*rankings: Iterable[FactId]) -> list[FactId]:
    return [fact_id for fact_id, _ in _rrf_scores(*rankings)]


def _rrf_scores(*rankings: Iterable[FactId]) -> list[tuple[FactId, float]]:
    scores: dict[FactId, float] = defaultdict(float)
    for ranking in rankings:
        for rank, fact_id in enumerate(ranking, start=1):
            scores[fact_id] += 1 / (_RRF_K + rank)
    return sorted(scores.items(), key=lambda hit: (-hit[1], hit[0].value))


def _candidates_within_boost(
    ranking: list[tuple[FactId, float]], limit: int
) -> list[tuple[FactId, float]]:
    if len(ranking) <= limit:
        return ranking
    cutoff = ranking[limit - 1][1]
    # 原榜候选的分只增不减，因此最高加成仍达不到原榜线的候选可省略。
    return [hit for hit in ranking if hit[1] * (1 + _MAX_RANKING_BOOST) >= cutoff]


def _recency_bonus(valid_at: float | None, reference_time: float) -> float:
    if valid_at is None:
        return 0.0
    elapsed = reference_time - valid_at
    return _MAX_RANKING_BOOST * exp2(-elapsed / _RECENCY_HALF_LIFE_SECONDS)


def _score_with_boosts(
    score: float, recency_bonus: float, normalized_salience: float
) -> float:
    return score * (
        1
        + min(
            _MAX_RANKING_BOOST, recency_bonus + _MAX_RANKING_BOOST * normalized_salience
        )
    )


def _relevance_scores(
    candidates: Sequence[FactId], ranked: object
) -> dict[FactId, float]:
    if (
        not isinstance(ranked, Sequence)
        or isinstance(ranked, (str, bytes, bytearray))
        or len(ranked) != len(candidates)
    ):
        raise InferenceError("重排须为每条候选返回一个相关性分数")
    allowed = set(candidates)
    scores: dict[FactId, float] = {}
    for item in ranked:
        if not isinstance(item, tuple) or len(item) != 2:
            raise InferenceError("重排须返回事实标识与相关性分数对")
        fact_id, score = item
        if (
            not isinstance(fact_id, FactId)
            or fact_id not in allowed
            or fact_id in scores
        ):
            raise InferenceError("重排返回了重复或不属于本次候选的事实标识")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
        ):
            raise InferenceError("重排相关性分数须为 0 到 1 之间的有限数")
        scores[fact_id] = float(score)
    return scores
