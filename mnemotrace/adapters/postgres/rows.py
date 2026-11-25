"""转换 SQL 行、JSONB 快照和领域对象。"""

from typing import TypedDict, Unpack

from ...domain.entity import Entity, EntityKind
from ...domain.enums import Role, Source
from ...domain.fact import Fact
from ...domain.ids import EntityId, FactId, MessageId, SessionId, SpaceId
from ...domain.message import Message
from ...domain.profile import Profile, ProfileEntry
from ...domain.salience import FactSalience
from ...domain.space import Space
from ...domain.values import EntityRef, Label, LiteralValue, Predicate
from ...ports.storage import CutoffTrigger, FactChange, GraphEdge

INSERT_SPACE = """
    INSERT INTO mnemotrace.spaces
        (space_id, owner_id, name, created_at, memory_decay, profile_auto_refresh,
         session_summary_enabled)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """


def space_insert_values(
    space: Space,
) -> tuple[str, str, str, float, bool, bool, bool]:
    """按空间写入语句的列顺序取出领域值。"""
    return (
        space.id.value,
        space.owner_id,
        space.name,
        space.created_at,
        space.memory_decay,
        space.profile_auto_refresh,
        space.session_summary_enabled,
    )


def space_from_row(
    space_id: str,
    owner_id: str,
    name: str,
    created_at: float,
    memory_decay: bool,
    profile_auto_refresh: bool,
    session_summary_enabled: bool,
) -> Space:
    """从空间表还原领域快照。

    Args:
        space_id: 持久化的空间标识。
        owner_id: 所有者账号标识。
        name: 空间显示名称。
        created_at: 创建时刻，单位为 UTC epoch 秒。
        memory_decay: 是否启用记忆衰减和自动强化。
        profile_auto_refresh: 是否按事实变化自动刷新画像。
        session_summary_enabled: 是否后台更新会话滚动摘要。

    Returns:
        空间快照。
    """
    return Space(
        SpaceId(space_id),
        owner_id,
        name,
        created_at,
        memory_decay,
        profile_auto_refresh,
        session_summary_enabled,
    )


def message_from_row(
    message_id: str,
    space_id: str,
    session_id: str,
    role: str,
    content: str,
    event_time: float | None,
    recorded_at: float,
) -> Message:
    """从消息表还原会话原文。

    Args:
        message_id: 持久化的原文标识。
        space_id: 原文所属空间的标识。
        session_id: 原文所属会话的标识。
        role: 持久化的发言角色。
        content: 未经改写的正文。
        event_time: 消息事件时刻，缺失时为 None。
        recorded_at: 首次收录时刻，单位为 UTC epoch 秒。

    Returns:
        原文快照。
    """
    return Message(
        MessageId(message_id),
        SpaceId(space_id),
        SessionId(session_id),
        Role(role),
        content,
        event_time,
        recorded_at,
    )


def entity_from_row(space_id: str, name: str, type: str | None, kind: str) -> Entity:
    """从名称和身份类型还原实体及其确定性标识。

    Args:
        space_id: 实体所属空间的标识。
        name: 实体原名称。
        type: 实体种类，缺失时为 None。
        kind: 持久化的实体身份类型。

    Returns:
        含派生标识和规范化名称的实体。
    """
    return Entity(SpaceId(space_id), name, type, kind=EntityKind(kind))


def graph_edge_from_row(
    fact_id: str, subject_id: str, object_entity_id: str
) -> GraphEdge:
    """还原用于图遍历的关系边。

    Args:
        fact_id: 支撑关系的事实标识。
        subject_id: 主体实体标识。
        object_entity_id: 客体实体标识。

    Returns:
        有向关系边。
    """
    return GraphEdge(FactId(fact_id), EntityId(subject_id), EntityId(object_entity_id))


class FactRow(TypedDict):
    """事实列和变更快照共用的字段布局。"""

    space_id: str
    fact_id: str
    subject_id: str
    predicate: str
    object_entity_id: str | None
    object_value: str | None
    object_kind: str | None
    text: str
    valid_at: float | None
    invalid_at: float | None
    recorded_at: float
    expired_at: float | None
    labels: list[str]
    sensitive: bool
    source: str
    provenance: list[str]


def fact_from_row(**row: Unpack[FactRow]) -> Fact:
    """从事实列或保存的快照还原领域事实。

    Args:
        **row: FactRow 声明的字段，来源及前驱按保存顺序传入。

    Returns:
        已还原的事实，包括有序引用和派生的正文指纹。
    """
    object_entity_id = row["object_entity_id"]
    if object_entity_id is None:
        object_value = row["object_value"]
        assert object_value is not None
        target: EntityRef | LiteralValue = LiteralValue(
            object_value, row["object_kind"]
        )
    else:
        target = EntityRef(EntityId(object_entity_id))
    return Fact(
        id=FactId(row["fact_id"]),
        space_id=SpaceId(row["space_id"]),
        subject=EntityRef(EntityId(row["subject_id"])),
        predicate=Predicate(row["predicate"]),
        object=target,
        text=row["text"],
        valid_at=row["valid_at"],
        invalid_at=row["invalid_at"],
        recorded_at=row["recorded_at"],
        expired_at=row["expired_at"],
        provenance=tuple(map(MessageId, row["provenance"])),
        labels=tuple(map(Label, row["labels"])),
        sensitive=row["sensitive"],
        source=Source(row["source"]),
    )


def fact_to_row(fact: Fact) -> FactRow:
    """提取 SQL 写入和历史快照共用的字段。

    Args:
        fact: 待转换的事实。

    Returns:
        可绑定或序列化的字段映射，派生字段由读取时的领域构造器重建。
    """
    target = fact.object
    return {
        "space_id": fact.space_id.value,
        "fact_id": fact.id.value,
        "subject_id": fact.subject.entity_id.value,
        "predicate": fact.predicate.value,
        "object_entity_id": target.entity_id.value
        if isinstance(target, EntityRef)
        else None,
        "object_value": target.value if isinstance(target, LiteralValue) else None,
        "object_kind": target.kind if isinstance(target, LiteralValue) else None,
        "text": fact.text,
        "valid_at": fact.valid_at,
        "invalid_at": fact.invalid_at,
        "recorded_at": fact.recorded_at,
        "expired_at": fact.expired_at,
        "labels": [label.value for label in fact.labels],
        "sensitive": fact.sensitive,
        "source": fact.source.value,
        "provenance": [source.value for source in fact.provenance],
    }


class _CutoffTriggerRow(TypedDict):
    fact_id: str
    valid_end: float
    source_message_ids: list[str]


def change_from_row(
    cursor: int,
    changed_at: float,
    before: FactRow | None,
    after: FactRow,
    triggers: list[_CutoffTriggerRow],
) -> FactChange:
    """从变更表还原一条历史记录。

    Args:
        cursor: 数据库分配的变更游标。
        changed_at: 变更时刻，单位为 UTC epoch 秒。
        before: 变更前快照，创建事实时为 None。
        after: 变更后快照。
        triggers: 按裁决顺序排列的截止触发项。

    Returns:
        包含前后事实快照的变更记录。
    """
    cutoff_triggers = tuple(
        CutoffTrigger(
            FactId(trigger["fact_id"]),
            trigger["valid_end"],
            tuple(map(MessageId, trigger["source_message_ids"])),
        )
        for trigger in triggers
    )
    return FactChange(
        cursor,
        changed_at,
        fact_from_row(**before) if before is not None else None,
        fact_from_row(**after),
        cutoff_triggers,
    )


class _ProfileEntryRow(TypedDict):
    text: str
    fact_ids: list[str]


class _ProfileGroup(TypedDict):
    group: str
    entries: list[_ProfileEntryRow]


def profile_to_json(profile: Profile) -> list[_ProfileGroup]:
    """把画像内容转换为有序的 JSON 数据。

    Args:
        profile: 待序列化的画像。

    Returns:
        可保存到 JSONB 的分组列表，保留分组、条目及引用顺序。
    """
    return [
        {
            "group": name,
            "entries": [
                {
                    "text": entry.text,
                    "fact_ids": [fact_id.value for fact_id in entry.fact_ids],
                }
                for entry in entries
            ],
        }
        for name, entries in profile.data.items()
    ]


def profile_from_row(
    space_id: str, data: list[_ProfileGroup], updated_at: float
) -> Profile:
    """从数据库行还原空间画像。

    Args:
        space_id: 画像所属空间的标识。
        data: 从 JSONB 读取的有序分组。
        updated_at: 最近更新时刻，单位为 UTC epoch 秒。

    Returns:
        包含有序分组及派生来源列表的画像快照。
    """
    groups = {
        group["group"]: tuple(
            ProfileEntry(entry["text"], tuple(map(FactId, entry["fact_ids"])))
            for entry in group["entries"]
        )
        for group in data
    }
    return Profile(SpaceId(space_id), groups, updated_at)


def salience_from_row(
    fact_id: str, salience: float | None, last_access: float | None
) -> tuple[FactId, FactSalience]:
    """还原可见事实的访问强化状态。

    Args:
        fact_id: 可见事实的标识。
        salience: 已保存的显著度，缺失时为 None。
        last_access: 最近强化时刻，尚未强化时为 None。

    Returns:
        事实标识及显著度状态。没有存储记录时使用领域基线。
    """
    state = FactSalience() if salience is None else FactSalience(salience, last_access)
    return FactId(fact_id), state
