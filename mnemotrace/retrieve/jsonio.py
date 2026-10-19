"""从 LLM 输出中稳健地提取 JSON —— 容忍代码围栏和围绕的散文。

LLM 会把 JSON 包在 ```json 围栏里、在前面加解释、或在结尾加句号。这些助手从噪声中拉出第一个
格式良好的数组/对象,失败时回退为 None,因此话痨或畸形的模型响应会退化为"无计划",而不是让读
路径崩溃。由 multi-hop 规划器共享;agentic 检索器在内联中使用相同的解析形状。
"""
from __future__ import annotations

import json
import re
from typing import Optional


def _strip_fences(raw: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()


def loads_array(raw: str) -> Optional[list]:
    """`raw` 中第一个 JSON 数组,若没有可解析的则返回 None。"""
    if not raw:
        return None
    text = _strip_fences(raw)
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return None


def loads_object(raw: str) -> Optional[dict]:
    """`raw` 中第一个 JSON 对象,若没有可解析的则返回 None。"""
    if not raw:
        return None
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None
