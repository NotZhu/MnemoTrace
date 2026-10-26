"""事实分类与画像分组共用的关键词边界；不用于敏感检测。"""
from __future__ import annotations

import re

CompiledKeywords = tuple[tuple[str, re.Pattern[str] | None], ...]


def normalize_predicate(predicate: str) -> str:
    return re.sub(r"[\s_-]+", "_", predicate.strip().lower())


def compile_keywords(keywords: tuple[str, ...]) -> CompiledKeywords:
    """静态词表在模块初始化时编译；等价分隔形式只保留一项。"""
    compiled = {}
    for keyword in keywords:
        if keyword.isascii():
            keyword = normalize_predicate(keyword)
            if keyword in compiled:
                continue
            pattern = r"[\s_-]+".join(re.escape(part) for part in keyword.split("_"))
            compiled[keyword] = re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")
        else:
            compiled[keyword] = None
    return tuple(compiled.items())


def matches_keywords(text: str, keywords: CompiledKeywords) -> bool:
    text = text.lower()
    for keyword, pattern in keywords:
        if pattern is not None:
            # 复合词不能字面预筛：blood_type 也须匹配 blood  type / blood-type。
            if "_" not in keyword and keyword not in text:
                continue
            if pattern.search(text):
                return True
        elif len(keyword) == 1:
            # 裸值“茶”“书”有明确含义；“资料”“秘书”等词中的单字不构成证据。
            if text.strip(" \t\r\n.,!?;:，。！？；：") == keyword:
                return True
        elif keyword in text:
            return True
    return False
