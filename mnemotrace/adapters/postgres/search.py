"""在当前连接和快照内计算向量与词法候选。"""

from collections.abc import Sequence
from itertools import groupby, pairwise
from unicodedata import name, normalize

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, args_row

from ...domain.ids import FactId, SpaceId
from ...domain.vector import scale_by_max_abs
from ...ports.storage import validate_embedding_dimension
from .fact_filters import FactConditions


def cosine_vector(vector: Sequence[float]) -> list[float]:
    """等比缩放向量，避免 pgvector 单精度计算的溢出和下溢。

    Args:
        vector: 待校验的向量分量。

    Returns:
        与输入向量同方向、最大分量绝对值为 1 的列表。

    Raises:
        ValueError: 向量为空、维度超过上限、含非有限分量或所有分量为零。
    """
    validate_embedding_dimension(len(vector))
    return scale_by_max_abs(vector)


async def embedding_dim(
    conn: AsyncConnection[TupleRow], space_id: SpaceId, embedding_space: str
) -> int | None:
    """读取空间内指定模型的登记维度。

    Args:
        conn: 当前会话的连接。
        space_id: 查询限定的空间标识。
        embedding_space: 向量模型及版本标识。

    Returns:
        模型的登记维度，尚未登记时为 None。

    Raises:
        ValueError: 嵌入空间标识为空白。
    """
    if not embedding_space.strip():
        raise ValueError("embedding_space 须为非空标识")
    async with conn.cursor(row_factory=args_row(int)) as cursor:
        await cursor.execute(
            """
            SELECT dim FROM mnemotrace.embedding_spaces
            WHERE space_id = %s AND embedding_space = %s
            """,
            (space_id.value, embedding_space),
        )
        return await cursor.fetchone()


def _word_kind(character: str) -> str:
    if character == "〇" or name(character, "").startswith("CJK UNIFIED IDEOGRAPH-"):
        return "han"
    return "word" if character.isalnum() else "separator"


def lexical_terms(text: str, *, query: bool = False) -> list[str]:
    """将归一化后的文本切分为词法索引用词项。

    Args:
        text: 待切分的正文或查询。
        query: 为 True 时，连续汉字以相邻双字切分，单独出现的汉字保留。

    Returns:
        汉字词项及其他连续字母数字词项，保留重复项。
    """
    terms: list[str] = []
    for kind, characters in groupby(normalize("NFKC", text).casefold(), key=_word_kind):
        if kind == "han":
            run = list(characters)
            if not query or len(run) == 1:
                terms.extend(run)
            terms.extend(left + right for left, right in pairwise(run))
        elif kind == "word":
            terms.append("".join(characters))
    return terms


def _hit(fact_id: str, score: float) -> tuple[FactId, float]:
    return FactId(fact_id), score


_RANK_HITS = sql.SQL("""
    SELECT fact_id, score FROM scored
    WHERE %(min_score)s::double precision IS NULL OR score >= %(min_score)s
    ORDER BY score DESC, fact_id ASC LIMIT %(limit)s
    """)

_VECTOR_SEARCH = sql.SQL("""
    WITH eligible AS MATERIALIZED (
        SELECT v.fact_id, v.embedding
        FROM mnemotrace.fact_vectors AS v
        JOIN mnemotrace.facts AS f
          ON f.space_id = v.space_id AND f.fact_id = v.fact_id
        WHERE {} AND v.embedding_space = %(embedding_space)s
    ), scored AS (
        SELECT fact_id,
               1 - (embedding OPERATOR(public.<=>) %(vector)s::float8[]::public.vector)
                   AS score
        FROM eligible
    )
    """)


async def search_vectors(
    conn: AsyncConnection[TupleRow],
    conditions: FactConditions,
    vector: Sequence[float],
    embedding_space: str,
    min_score: float | None,
    limit: int,
) -> list[tuple[FactId, float]]:
    """在符合事实条件的同模型向量中执行精确余弦检索。

    Args:
        conn: 当前会话的连接。
        conditions: 含空间约束的事实筛选条件。
        vector: 已校验并缩放的查询向量。
        embedding_space: 查询对应的嵌入空间标识。
        min_score: 包含下界的最低分数，None 表示不限。
        limit: 最大候选数。

    Returns:
        按分数倒序排列的事实标识和分数，同分按事实标识升序排列。
    """
    statement = _VECTOR_SEARCH.format(conditions.where()) + _RANK_HITS
    async with conn.cursor(row_factory=args_row(_hit)) as cursor:
        await cursor.execute(
            statement,
            {
                **conditions.params,
                "vector": vector,
                "embedding_space": embedding_space,
                "min_score": min_score,
                "limit": limit,
            },
        )
        return await cursor.fetchall()


async def search_lexical(
    conn: AsyncConnection[TupleRow],
    conditions: FactConditions,
    query: str,
    min_score: float | None,
    limit: int,
) -> list[tuple[FactId, float]]:
    """按词项命中和词法得分选择事实候选。

    Args:
        conn: 当前会话的连接。
        conditions: 含空间约束的事实筛选条件。
        query: 待分词的查询文本。
        min_score: 包含下界的最低分数，None 表示不限。
        limit: 最大候选数。

    Returns:
        按分数倒序排列的事实标识和分数，同分按事实标识升序排列。
    """
    terms = lexical_terms(query, query=True)
    if not terms:
        return []
    statement = sql.SQL("""
        WITH query AS (
            SELECT to_tsquery('pg_catalog.simple', %(query)s) AS terms
        ), scored AS (
            SELECT f.fact_id, ts_rank(f.lexemes, query.terms)::double precision AS score
            FROM mnemotrace.facts AS f CROSS JOIN query
            WHERE {} AND f.lexemes @@ query.terms
        ) {}
        """).format(conditions.where(), _RANK_HITS)
    async with conn.cursor(row_factory=args_row(_hit)) as cursor:
        # 分词结果仅包含字母数字或汉字，竖线由适配器加入。
        await cursor.execute(
            statement,
            {
                **conditions.params,
                "query": "|".join(dict.fromkeys(terms)),
                "min_score": min_score,
                "limit": limit,
            },
        )
        return await cursor.fetchall()
