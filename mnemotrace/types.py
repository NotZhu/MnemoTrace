"""双时间轴数据模型。见 AGENTS.md 「对象与时间语义」。

核心不变式：被反驳的 fact 做*失效化*，绝不硬删除。每个 Fact 跟踪两条独立的时间轴，
使我们既能回答"现在什么是真的？"，也能回答"在时刻 T 我们认为什么是真的？"：

  * valid time      (valid_at / invalid_at)  -- 该 fact 在世界中为真的时间
  * transaction time (created_at / expired_at) -- *我们*得知 / 撤回它的时间
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .topics import Topic, TopicSource, validate_topic
from .util import gen_id, now


@dataclass
class Episode:
    """原始、无损的一轮对话或事件。写入后永不修改（append-only 日志）。"""

    content: str
    user_id: str = "default"
    session_id: str = "default"
    speaker: str = "user"
    event_time: float = field(default_factory=now)  # 在世界中发生的时间
    ingested_at: float = field(default_factory=now)  # 我们记录它的时间
    embedding: Optional[list[float]] = None
    consolidated: bool = False
    # L2 抽象：该 episode/session 的紧凑摘要，整合时计算一次，读取时用它（而非完整原文）
    # 构建精简上下文——这是让一小片检索结果就能廉价覆盖大量 session 的扩展性基石（AGENTS.md Bet A/E）。
    summary: str = ""
    summary_embedding: Optional[list[float]] = None
    id: str = field(default_factory=lambda: gen_id("ep"))
    metadata: dict = field(default_factory=dict)


@dataclass
class Fact:
    """原子化的 (subject, predicate, object) 断言，带双时间轴有效性 + 来源溯源。"""

    subject: str
    predicate: str
    object: str
    text: str = ""  # 规范的英文 predicate 渲染，如 "Wei works_at Moonshot AI"（用于嵌入/检索）
    # 抽取时产出的原语言一行表述（如"李雷在字节跳动工作"），在 UI 中展示。旧 fact / 离线抽取
    # 为空 => 由展示层基于 predicate 本地化。
    display: str = ""
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
    category: str = ""
    sensitive: bool = False

    # --- 有效时间（世界）---
    valid_at: float = field(default_factory=now)
    invalid_at: Optional[float] = None  # None => 在世界中仍为真

    # --- 事务时间（认知）---
    created_at: float = field(default_factory=now)
    expired_at: Optional[float] = None  # None => 仍被认为成立

    supersedes: Optional[str] = None  # 本 fact 所取代的 fact 的 id（演化链）
    provenance: list[str] = field(default_factory=list)  # 来源 Episode 的 id 列表

    last_access: float = field(default_factory=now)
    access_count: int = 0
    id: str = field(default_factory=lambda: gen_id("ft"))

    # 与事实来源、管理分类及敏感标记独立；自动赋值由写入路径接入。
    topic: Topic = "unknown"
    topic_source: TopicSource = "unset"

    def __post_init__(self) -> None:
        validate_topic(self.topic, self.topic_source)
        if not self.text:
            # 把 predicate 渲染成自然语言（"works_at" -> "works at"），让嵌入和词汇匹配
            # 看到的是真实文本，而不是 snake_case token。
            readable = self.predicate.replace("_", " ")
            self.text = f"{self.subject} {readable} {self.object}".strip()

    @property
    def slot(self) -> tuple[str, str, str]:
        """(user, subject, predicate) 键。共享同一槽位的两个 fact 是候选冲突。"""
        return (self.user_id, self.subject.lower(), self.predicate.lower())

    def is_live(self, as_of: Optional[float] = None) -> bool:
        """当该 fact 在 `as_of` 时刻既仍被认知、又在世界中有效时返回 True。"""
        t = now() if as_of is None else as_of
        believed = self.expired_at is None or self.expired_at > t
        valid = self.valid_at <= t and (self.invalid_at is None or self.invalid_at > t)
        return believed and valid


@dataclass
class Entity:
    """语义图中的节点。参考实现按 (user_id, 小写名) 解析。"""

    name: str
    type: str = "entity"
    user_id: str = "default"
    embedding: Optional[list[float]] = None
    aliases: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: gen_id("en"))


@dataclass
class Relation:
    """两个实体之间的有向双时间轴边，由一个 Fact 支撑。"""

    subject_id: str
    predicate: str
    object_id: str
    fact_id: str
    valid_at: float = field(default_factory=now)
    invalid_at: Optional[float] = None
    id: str = field(default_factory=lambda: gen_id("rel"))


@dataclass
class WorkingMemory:
    """短期工作记忆（AGENTS.md 「架构概览与代码导航」 typed memory）：绝不能污染持久长期存储的瞬态状态。
    绑定到 session 和/或硬性 TTL，在所属 session 结束、到期或被消费时清除。
    例如："今天嗓子疼"（瞬态状态）、"提醒我买牛奶"（意图）、单次行程中的同行者信息。
    与 `working_set`（查询的瞬态注意力集合）不同——这是一个持久化、有生命周期管理的层级。

    生命周期（通用记忆卫生规则——瞬态上下文不得进入长期存储）：
      * session-scoped  -> 存活到 clear_session()（例如一次新对话 / 重启）
      * ttl/expires_at  -> 在墙钟时间硬性过期（例如带日期的提醒、2 小时回退）
      * consumed        -> 完成使命后软清除
    """

    content: str
    user_id: str = "default"
    session_id: str = "default"
    kind: str = "state"  # state | intent | schedule | note | passenger | ...
    event_time: float = field(default_factory=now)
    created_at: float = field(default_factory=now)
    expires_at: Optional[float] = None  # 硬性墙钟过期时间；None => 仅随 session 存活
    consumed: bool = False  # 被使用后软清除
    embedding: Optional[list[float]] = None
    id: str = field(default_factory=lambda: gen_id("wm"))
    metadata: dict = field(default_factory=dict)

    def is_live(self, as_of: Optional[float] = None, session_id: Optional[str] = None) -> bool:
        t = now() if as_of is None else as_of
        if self.consumed:
            return False
        if self.expires_at is not None and self.expires_at <= t:
            return False
        if session_id is not None and self.session_id != session_id:
            return False
        return True


@dataclass
class Conflict:
    """用户两条 fact 之间的可疑冲突，呈现给用户确认而非自动解决（这是廉价规则覆盖不了的
    模糊尾部的安全路径：System-2 中的 LLM 只负责检测，由用户裁决）。`older`/`newer` 按
    valid time 排序；解决时保留一方并取代另一方。绝不自动应用——杜绝悄悄的损坏。"""

    older: str  # valid_at 较早的 fact id（疑似过时方）
    newer: str  # valid_at 较晚的 fact id（疑似现行方）
    text_older: str = ""  # 展示快照（fact 之后可能被编辑/删除）
    text_newer: str = ""
    user_id: str = "default"
    reason: str = ""
    status: str = "pending"  # pending | resolved | dismissed
    detected_at: float = field(default_factory=now)
    id: str = field(default_factory=lambda: gen_id("cf"))

    @property
    def pair_key(self) -> tuple:
        return tuple(sorted((self.older, self.newer)))
