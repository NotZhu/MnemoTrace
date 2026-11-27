"""提供嵌入向量的归一化运算。"""

from collections.abc import Sequence
from math import hypot, isfinite


def scale_by_max_abs(vector: Sequence[float]) -> list[float]:
    """把有限的非零向量缩放到最大分量绝对值为 1。

    Args:
        vector: 待归一化的嵌入向量。

    Returns:
        与输入向量方向相同的浮点列表。

    Raises:
        ValueError: 向量为空、含非有限分量或所有分量均为零。
    """
    if not vector or not all(map(isfinite, vector)):
        raise ValueError("向量须包含有限分量")
    scale = max(map(abs, vector))
    if scale == 0:
        raise ValueError("余弦向量的范数须大于零")
    return [component / scale for component in vector]


def unit_vector(vector: Sequence[float]) -> tuple[float, ...]:
    """把有限的非零向量缩放为同方向的单位向量。

    Args:
        vector: 待归一化的嵌入向量。

    Returns:
        可用于计算余弦相似度的单位向量。

    Raises:
        ValueError: 向量为空、含非有限分量或所有分量均为零。
    """
    # 先等比缩放，使大分量的有限向量也能计算有限的范数。
    scaled = scale_by_max_abs(vector)
    norm = hypot(*scaled)
    return tuple(component / norm for component in scaled)
