"""BM25 词法打分,带轻量词干化,使 "work" 能匹配 "works_at",而无需训练模型。"""
from __future__ import annotations

import math
import unicodedata

from ..util import stem, stems

__all__ = ["stem", "stems", "token_overlap", "bm25_scores"]
BM25_K1 = 1.5
BM25_B = 0.75
_BM25_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'am', 'is', 'are', 'was', 'were', 'be', 'been',
    'do', 'does', 'did', 'i', 'me', 'my', 'we', 'our', 'you', 'your',
    'in', 'at', 'of', 'on', 'to', 'for', 'what', 'where', 'when', 'which', 'who', 'how',
})


def normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def lexical_terms(text: str, *, query: bool = False, exact: bool = False) -> list[str]:
    """Analyze a document or query without changing the stored text."""
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


def overlap_terms(query: str, text: str) -> set[str]:
    return set(lexical_terms(query, query=True)) & set(lexical_terms(text))


def token_overlap(query: str, text: str) -> int:
    return len(overlap_terms(query, text))


def bm25_query_terms(query: str) -> set[str]:
    return set(lexical_terms(query, query=True)) - _BM25_STOP_WORDS


def bm25_scores(query: str, docs: list[tuple[str, str]], k1: float = BM25_K1, b: float = BM25_B) -> dict[str, float]:
    """docs:(id, text) 列表。返回 {id: bm25 分数}。"""
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
