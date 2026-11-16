"""BM25 词法打分,带轻量词干化,使 "work" 能匹配 "works_at",而无需训练模型。"""
from __future__ import annotations

import math
import re
import unicodedata

from ..util import stem

__all__ = ["stem", "lexical_terms", "token_overlap", "bm25_scores"]
BM25_K1 = 1.5
BM25_B = 0.75
_BM25_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'am', 'is', 'are', 'was', 'were', 'be', 'been',
    'do', 'does', 'did', 'i', 'me', 'my', 'we', 'our', 'you', 'your',
    'in', 'at', 'of', 'on', 'to', 'for', 'what', 'where', 'when', 'which', 'who', 'how',
})


def normalize(text: str) -> str:
    """规范化 Unicode 文本并折叠大小写。

    Args:
        text: 待分析的文本。

    Returns:
        使用 NFKC 和大小写折叠后的文本。
    """
    return unicodedata.normalize('NFKC', text).casefold()


def lexical_terms(text: str, *, query: bool = False, exact: bool = False) -> list[str]:
    """将文本切分为词项，汉字串按文档或查询模式处理。

    Args:
        text: 待分析的文本。
        query: 是否使用查询分词模式。
        exact: 是否保留 ASCII 字母词项的原形。

    Returns:
        按出现顺序排列的词项，保留重复项。
    """
    terms = []
    word = []
    han = []

    def flush_word():
        if word:
            token = ''.join(word)
            terms.append(stem(token) if not exact and token.isascii() and token.isalpha() else token)
            word.clear()

    def flush_han():
        if not query or len(han) == 1:
            terms.extend(han)
        terms.extend(left + right for left, right in zip(han, han[1:]))
        han.clear()

    for char in normalize(text):
        is_han = char == '〇' or unicodedata.name(char, '').startswith('CJK UNIFIED IDEOGRAPH-')
        if is_han:
            flush_word()
            han.append(char)
            continue
        if han:
            flush_han()
        if char.isalnum() or (word and unicodedata.category(char).startswith('M')):
            word.append(char)
        else:
            flush_word()
    flush_word()
    flush_han()
    return terms


def literal_value_match(query: str, value: str) -> bool:
    """检查查询中是否完整出现字母与数字混合的字面值。

    Args:
        query: 查询文本。
        value: 候选事实的对象值。

    Returns:
        值满足格式并完整匹配时返回 True。
    """
    value = value.strip().casefold()
    if (not value.isascii() or any(char.isspace() for char in value)
            or not any(char.isalpha() for char in value) or not any(char.isdigit() for char in value)):
        return False
    pattern = r'(?<!\w)(?<!\w[./-])' + re.escape(value) + r'(?!\w)(?![./-]\w)'
    return re.search(pattern, query.casefold()) is not None


def overlap_terms(query: str, text: str) -> set[str]:
    """提取查询与文本共有的词项。

    Args:
        query: 查询文本。
        text: 待比较的文本。

    Returns:
        共同的词项集合。
    """
    return set(lexical_terms(query, query=True)) & set(lexical_terms(text))


def token_overlap(query: str, text: str) -> int:
    return len(overlap_terms(query, text))


def bm25_query_terms(query: str) -> set[str]:
    """提取 BM25 使用的查询词项。

    Args:
        query: 查询文本。

    Returns:
        去重并移除停用词后的查询词项。
    """
    return set(lexical_terms(query, query=True)) - _BM25_STOP_WORDS


def bm25_scores(query: str, docs: list[tuple[str, str]], k1: float = BM25_K1, b: float = BM25_B) -> dict[str, float]:
    """在给定文档集合上计算 BM25 分数。

    Args:
        query: 查询文本。
        docs: 文档 ID 与文本的二元组列表。
        k1: 词频饱和参数。
        b: 文档长度归一化参数。

    Returns:
        文档 ID 到 BM25 分数的映射。
    """
    if not docs:
        return {}
    doc_tokens = {doc_id: lexical_terms(text) for doc_id, text in docs}
    n = len(docs)
    avgdl = max(1.0, sum(len(t) for t in doc_tokens.values()) / n)
    df: dict[str, int] = {}
    for toks in doc_tokens.values():
        for w in set(toks):
            df[w] = df.get(w, 0) + 1
    q_terms = bm25_query_terms(query)
    scores: dict[str, float] = {}
    for doc_id, toks in doc_tokens.items():
        if not toks:
            scores[doc_id] = 0.0
            continue
        tf: dict[str, int] = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        dl = len(toks)
        s = 0.0
        for w in q_terms:
            if w not in tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * (tf[w] * (k1 + 1)) / (tf[w] + k1 * (1 - b + b * dl / avgdl))
        scores[doc_id] = s
    return scores
