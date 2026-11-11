"""双时间轴数据模型。见 AGENTS.md 「对象与时间语义」。

核心不变式：被反驳的 fact 做*失效化*，绝不硬删除。每个 Fact 跟踪两条独立的时间轴，
使我们既能回答"现在什么是真的？"，也能回答"在时刻 T 我们认为什么是真的？"：

  * valid time      (valid_at / invalid_at)  -- 该 fact 在世界中为真的时间
  * transaction time (created_at / expired_at) -- *我们*得知 / 撤回它的时间
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Literal, Optional, get_args

from .categories import Category, CategoriesSource, ObjectKind, validate_categories, validate_object_kind
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
    embedding: Optional[list[float]] = None
    consolidated: bool = False
    # L2 抽象：该 episode/session 的紧凑摘要，整合时计算一次，读取时用它（而非完整原文）
    # 构建精简上下文——这是让一小片检索结果就能廉价覆盖大量 session 的扩展性基石（AGENTS.md Bet A/E）。
    digest: str = ""
    digest_embedding: Optional[list[float]] = None
    id: str = field(default_factory=lambda: gen_id("ep"))
    metadata: dict = field(default_factory=dict)
    source_kind: str = "raw"
    scope: str = "long"
    message_id: Optional[str] = None
    sequence: Optional[int] = None
    mode: Optional[str] = None
    coverage: Optional[dict] = None
    digest_coverage: Optional[dict] = None


Cardinality = Literal["single", "multi", "unknown"]
Retention = Literal["durable", "normal", "unknown"]
CARDINALITIES = get_args(Cardinality)
RETENTIONS = get_args(Retention)


def validate_lifecycle(cardinality: Cardinality, retention: Retention) -> None:
    if cardinality not in CARDINALITIES:
        raise ValueError("Invalid fact cardinality")
    if retention not in RETENTIONS:
        raise ValueError("Invalid fact retention")


def validate_validity_revisions(revisions: object) -> None:
    if not isinstance(revisions, list):
        raise ValueError("validity_revisions must be a list")
    previous = float("-inf")
    for revision in revisions:
        if not isinstance(revision, dict) or set(revision) != {"changed_at", "invalid_at", "expired_at"}:
            raise ValueError("Invalid validity revision fields")
        for name, value in revision.items():
            if value is None and name != "changed_at":
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid validity revision timestamp")
        if revision["changed_at"] < previous:
            raise ValueError("Validity revisions must be ordered by change time")
        previous = revision["changed_at"]


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
    # 来源类别："extracted"（自动，来自对话）或 "user"（经管理界面手动断言/编辑）。
    # 用户断言的 fact 是权威的——自动抽取绝不能悄悄覆盖它（这样手动修正才能留存）。
    # 这是用户侧记忆编辑可信的基础。
    source: str = "extracted"
    # 分类（feature ⑤）：粗粒度的领域/类别标签和敏感标记（PII/健康/财务等）。
    # 默认值供离线构造与写入后的分类使用；敏感 fact 可在共享/导出场景中脱敏。
    sensitive: bool = False
    sensitive_override: bool = False
    review_pending: bool = False
    approved_at: Optional[float] = None

    # --- 有效时间（世界）---
    valid_at: Optional[float] = field(default_factory=now)
    invalid_at: Optional[float] = None  # None => 在世界中仍为真

    # --- 事务时间（认知）---
    created_at: float = field(default_factory=now)
    expired_at: Optional[float] = None  # None => 仍被认为成立
    validity_revisions: list[dict[str, float | None]] = field(default_factory=list)

    supersedes: Optional[str] = None  # 本 fact 所取代的 fact 的 id（演化链）
    provenance: list[str] = field(default_factory=list)  # 来源 Episode 的 id 列表

    last_access: float = field(default_factory=now)
    access_count: int = 0
    id: str = field(default_factory=lambda: gen_id("ft"))

    # 与事实来源、管理分类及敏感标记独立；自动赋值由写入路径接入。
    categories: list[Category] = field(default_factory=list)
    categories_source: CategoriesSource = "unset"
    object_kind: ObjectKind = "unknown"
    cardinality: Cardinality = "unknown"
    retention: Retention = "unknown"
    source_roles: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_categories(self.categories, self.categories_source)
        validate_object_kind(self.object_kind)
        validate_lifecycle(self.cardinality, self.retention)
        validate_validity_revisions(self.validity_revisions)
        for name in ('subject', 'predicate', 'object', 'text'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{name} must be a non-empty string')

    @property
    def slot(self) -> tuple[str, str, str]:
        """(user, subject, predicate) 键。共享同一槽位的两个 fact 是候选冲突。"""
        return (self.user_id, self.subject.strip().lower(), self.predicate.strip().lower())

    def is_published(self, known_at: Optional[float] = None) -> bool:
        return not self.review_pending and (self.approved_at is None or
                                            self.approved_at <= (now() if known_at is None else known_at))

    def revise_end_times(self, invalid_at: float | None, expired_at: float | None, *, changed_at: float) -> None:
        """Keep the interval known before each change for transaction-time reads."""
        if (self.invalid_at, self.expired_at) != (invalid_at, expired_at):
            revisions = [
                *self.validity_revisions,
                dict(changed_at=changed_at, invalid_at=self.invalid_at, expired_at=self.expired_at),
            ]
            validate_validity_revisions(revisions)
            self.validity_revisions = revisions
            self.invalid_at, self.expired_at = invalid_at, expired_at

    def as_known_at(self, known_at: float | None) -> Fact:
        if known_at is not None:
            for index, revision in enumerate(self.validity_revisions):
                if revision["changed_at"] > known_at:
                    return replace(self, invalid_at=revision["invalid_at"], expired_at=revision["expired_at"],
                                   validity_revisions=self.validity_revisions[:index])
        return self

    def is_valid_at(self, at: float) -> bool:
        """Whether the effective interval covers at, independent of publication and expiry."""
        return (self.valid_at is None or self.valid_at <= at) and (self.invalid_at is None or self.invalid_at > at)

    def is_live(self, as_of: Optional[float] = None) -> bool:
        """当该 fact 在 `as_of` 时刻既仍被认知、又在世界中有效时返回 True。"""
        t = now() if as_of is None else as_of
        believed = self.expired_at is None or self.expired_at > t
        return self.is_published(t) and believed and self.is_valid_at(t)


@dataclass
class Entity:
    """语义图中的节点。身份键是 (user_id, name)，不区分大小写。"""
    # 实体名
    name: str
    # 实体类型
    type: str = "entity"
    # 所属用户
    user_id: str = "default"
    # 实体向量
    embedding: Optional[list[float]] = None
    # 实体 id
    id: str = field(default_factory=lambda: gen_id("en"))


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

    older: str  # 有效时间较早的事实 ID
    newer: str  # 有效时间较晚的事实 ID
    text_older: str = ""  # 检测时旧事实的文本快照
    text_newer: str = ""  # 检测时新事实的文本快照
    user_id: str = "default"  # 所属用户 ID
    reason: str = ""  # 疑似冲突的原因
    status: str = "pending"  # pending 待处理；resolved 已裁决；dismissed 已排除
    detected_at: float = field(default_factory=now)  # 检测时间，默认为创建时刻
    id: str = field(default_factory=lambda: gen_id("cf"))  # 冲突记录 ID

    @property
    def pair_key(self) -> tuple:
        return tuple(sorted((self.older, self.newer)))
