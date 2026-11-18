"""定义事实筛选与分页参数。

请求入口解析并校验状态、布尔值、领域标识和元组元素。所有时刻均为有限的
UTC epoch 秒。
"""

from dataclasses import dataclass, field
from typing import Literal

from ..domain.ids import FactId, MessageId
from ..domain.values import EntityRef, Label, Predicate


@dataclass(frozen=True)
class FactFilter:
    """描述存储和检索共用的事实筛选条件。

    各字段条件取交集。时点取显式 valid_at，省略时使用调用传入的
    reference_time。显式查询要求事实的有效起点已知。

    Attributes:
        state: current 按左闭右开区间匹配该时点有效的事实，省略 valid_at 时
            允许起点未知。history 匹配截至该时点已截止的事实。all 读取完整
            时间线，包含未来生效和起点未知的事实，须省略 valid_at。
        valid_at: 显式有效时点，单位为 UTC epoch 秒。
        subject: 要匹配的主体。
        predicate: 要匹配的谓词。
        labels: 匹配任意一项即可的标签，空元组不限。
        provenance: 匹配任意一项即可的原文标识，空元组不限。
        exclude_sensitive: 是否排除敏感事实。受限读取由服务端设为真。

    Raises:
        ValueError: 读取完整时间线时同时指定了有效时点。
    """

    state: Literal["current", "history", "all"] = "current"
    valid_at: float | None = None
    subject: EntityRef | None = None
    predicate: Predicate | None = None
    labels: tuple[Label, ...] = ()
    provenance: tuple[MessageId, ...] = ()
    exclude_sensitive: bool = False

    def __post_init__(self) -> None:
        if self.state == "all" and self.valid_at is not None:
            raise ValueError("state 为 all 时须省略 valid_at")


@dataclass(frozen=True)
class FactCursor:
    """标记上一页最后一条事实的排序位置。

    Attributes:
        recorded_at: 事实首次收录的 UTC epoch 秒。
        fact_id: 同一收录时刻下用于排序的事实标识。
    """

    recorded_at: float
    fact_id: FactId


@dataclass(frozen=True)
class FactQuery:
    """读取一页符合筛选条件的事实。

    Attributes:
        filters: 本次查询使用的事实筛选条件。
        after: 从此位置之后读取，省略时读取首页。
        limit: 单页最多返回的条数，外部请求允许 1 至 1000。
    """

    filters: FactFilter = field(default_factory=FactFilter)
    after: FactCursor | None = None
    limit: int = 100
