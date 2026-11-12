"""Memory records and effective-time semantics."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal, Optional, get_args

from .categories import Category, CategoriesSource, validate_categories
from .util import gen_id, now


@dataclass
class Episode:
    """原始、无损的一轮对话或事件。写入后永不修改（append-only 日志）。"""

    content: str
    user_id: str = "default"
    session_id: str = "default"
    role: str = "user"
    event_time: Optional[float] = field(default_factory=now)  # None 明确表示来源时间未知
    ingested_at: float = field(default_factory=now)  # 我们记录它的时间
    consolidated: bool = False
    # L2 抽象：该 episode/session 的紧凑摘要，整合时计算一次，读取时用它（而非完整原文）
    # 构建精简上下文——这是让一小片检索结果就能廉价覆盖大量 session 的扩展性基石（AGENTS.md Bet A/E）。
    digest: str = ""
    digest_embedding: Optional[list[float]] = None
    id: str = field(default_factory=lambda: gen_id("ep"))
    metadata: dict = field(default_factory=dict)
    source_kind: str = "note"
    extract: bool = True
    message_id: Optional[str] = None
    sequence: Optional[int] = None
    coverage: Optional[dict] = None
    digest_coverage: Optional[dict] = None
    digest_written_at: Optional[float] = None

    def __post_init__(self):
        if self.source_kind not in {"note", "dialogue"}:
            raise ValueError("source_kind must be note or dialogue")
        if type(self.extract) is not bool:
            raise ValueError("extract must be a boolean")


Retention = Literal["durable", "normal", "unknown"]
RETENTIONS = get_args(Retention)


def validate_retention(retention: Retention) -> None:
    if retention not in RETENTIONS:
        raise ValueError("Invalid fact retention")


def relation_identifier(predicate: str) -> str:
    return "pred_" + sha256(predicate.encode()).hexdigest()


@dataclass
class Fact:
    """原子化的 (subject, predicate, object) 断言，带双时间轴有效性 + 来源溯源。"""

    subject: str
    predicate: str
    object: str
    text: str  # 自包含的事实表述；抽取保留原语言，展示与检索共用。
    user_id: str = "default"
    embedding: Optional[list[float]] = None

    salience: float = 1.0  # 重要程度；被访问时提升，随时间衰减
    confidence: float = 1.0
    # 来源类别：自动抽取，或离线构造的用户断言。
    source: str = "extracted"
    # 分类（feature ⑤）：粗粒度的领域/类别标签和敏感标记（PII/健康/财务等）。
    # 默认值供离线构造与写入后的分类使用；敏感 fact 可在共享/导出场景中脱敏。
    sensitive: bool = False
    sensitive_override: bool = False
    review_pending: bool = False
    withdrawn_at: float | None = None

    # --- 有效时间（世界）---
    valid_at: Optional[float] = field(default_factory=now)
    invalid_at: Optional[float] = None  # None => 在世界中仍为真

    # --- 事务时间（认知）---
    created_at: float = field(default_factory=now)
    expired_at: Optional[float] = None  # 首次获知有效区间结束

    supersedes: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)  # 来源 Episode 的 id 列表

    last_access: float = field(default_factory=now)
    access_count: int = 0
    id: str = field(default_factory=lambda: gen_id("ft"))

    # 与事实来源、管理分类及敏感标记独立；自动赋值由写入路径接入。
    categories: list[Category] = field(default_factory=list)
    categories_source: CategoriesSource = "unset"
    relation_id: str | None = None
    subject_id: str | None = None
    object_id: str | None = None
    subject_type: str | None = None
    object_type: str | None = None
    retention: Retention = "unknown"
    source_roles: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_categories(self.categories, self.categories_source)
        validate_retention(self.retention)
        if self.relation_id is None:
            self.relation_id = relation_identifier(self.predicate)
        if self.id in self.supersedes or len(set(self.supersedes)) != len(self.supersedes):
            raise ValueError("Invalid direct predecessors")
        for name in ('subject', 'predicate', 'object', 'text'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{name} must be a non-empty string')

    @property
    def subject_key(self) -> tuple[str | None, str | None]:
        return (self.subject_id, None) if self.subject_id else (None, self.subject.strip().casefold())

    @property
    def slot(self) -> tuple[str, tuple[str | None, str | None], str | None]:
        return self.user_id, self.subject_key, self.relation_id

    def is_published(self) -> bool:
        return not self.review_pending and self.withdrawn_at is None

    def end_at(self, effective: float, recorded: float) -> None:
        if self.invalid_at is None or effective < self.invalid_at:
            self.invalid_at = effective
            if self.expired_at is None:
                self.expired_at = recorded

    def is_valid_at(self, at: float) -> bool:
        return (self.valid_at is None or self.valid_at <= at) and (self.invalid_at is None or at < self.invalid_at)

    def is_live(self, as_of: float | None = None) -> bool:
        return (self.is_published() and (as_of is None or self.valid_at is not None)
                and self.is_valid_at(now() if as_of is None else as_of))


@dataclass
class Entity:
    """语义图中的节点。身份由空间内的 ID 区分。"""
    # 实体名
    name: str
    # 实体类型
    type: str | None = None
    # 所属用户
    user_id: str = "default"
    # 实体向量
    embedding: Optional[list[float]] = None
    # 实体 id
    id: str = field(default_factory=lambda: gen_id("en"))


@dataclass
class RelationType:
    predicate: str
    meaning: str
    user_id: str = "default"
    direction: Literal["forward", "symmetric"] = "forward"
    endpoint_types: list[list[str]] = field(default_factory=list)
    id: str | None = None

    def __post_init__(self):
        if self.id is None:
            self.id = relation_identifier(self.predicate)


@dataclass
class Relation:
    """两个实体之间的有向双时间轴边，由一个 Fact 支撑。"""
    # 主体 id
    subject_id: str
    # 谓词
    predicate: str
    # 宾体 id
    object_id: str
    # 支撑边的 fact id
    fact_id: str
    # 有效的时间点
    valid_at: Optional[float] = field(default_factory=now)
    # 无效的时间点
    invalid_at: Optional[float] = None
    # 边 id
    id: str = field(default_factory=lambda: gen_id("rel"))


@dataclass
class Conflict:
    """两条事实之间的疑似冲突，按有效时间区分较早和较晚的事实。

    LLM 检测出的冲突由用户处理：可保留一方、保留两方或排除该冲突。
    """

    older: str  # 已存的事实 ID
    newer: str  # 待比较的候选事实 ID
    text_older: str = ""  # 检测时旧事实的文本快照
    text_newer: str = ""  # 检测时新事实的文本快照
    user_id: str = "default"  # 所属用户 ID
    reason: str = ""  # 疑似冲突的原因
    older_version: int = 1
    newer_version: int = 1
    detected_at: float = field(default_factory=now)  # 检测时间，默认为创建时刻
    id: str = field(default_factory=lambda: gen_id("cf"))  # 冲突记录 ID

    @property
    def pair_key(self) -> tuple:
        return tuple(sorted((self.older, self.newer)))
