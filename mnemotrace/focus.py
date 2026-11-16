"""Shared focus selectors for facts, source text and SQL retrieval."""
import re

from .categories import CATEGORIES

TRACK_BOOST = 0.5


def selectors(terms):
    """将关注项解析为文本匹配规则和类别筛选。

    Args:
        terms: 主题文本和带 category 前缀的类别。

    Returns:
        文本匹配表达式及类别列表。

    Raises:
        ValueError: 关注项包含未知类别。
    """
    patterns, categories = [], []
    for term in terms:
        term = term.strip().lower()
        if term.startswith('category:'):
            label = term.removeprefix('category:')
            if label not in CATEGORIES:
                raise ValueError('Unknown focus category')
            categories.append(label)
        elif term:
            escaped = ''.join(chr(92) + char if char in r"\.^$*+?{}[]()|" else char for char in term)
            if re.match(r'[a-z0-9_]', term):
                escaped = r'(?<![a-z0-9_])' + escaped
            if re.search(r'[a-z0-9_]$', term):
                escaped += r'(?![a-z0-9_])'
            patterns.append(escaped)
    return patterns, categories


def matches(text, terms, categories=()):
    """检查文本或类别是否匹配任一关注项。

    Args:
        text: 要检查的事实或原文文本。
        terms: 追踪或屏蔽项。
        categories: 文本关联的事实类别。

    Returns:
        有任一匹配时返回 True。
    """
    if not terms:
        return False
    patterns, labels = selectors(terms)
    return bool(set(labels).intersection(categories) or any(re.search(p, text.lower()) for p in patterns))
