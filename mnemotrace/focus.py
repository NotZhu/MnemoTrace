"""Shared focus selectors for facts, source text and SQL retrieval."""
import re

from .categories import CATEGORIES

TRACK_BOOST = 0.5


def selectors(terms):
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
    if not terms:
        return False
    patterns, labels = selectors(terms)
    return bool(set(labels).intersection(categories) or any(re.search(p, text.lower()) for p in patterns))
