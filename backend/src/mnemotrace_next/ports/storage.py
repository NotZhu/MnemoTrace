"""定义空间存储及事务会话端口。"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, overload

from ..domain.digest import Digest
from ..domain.entity import Entity
from ..domain.fact import Fact
from ..domain.ids import DigestId, EntityId, FactId, MessageId, SessionId, SpaceId
from ..domain.message import Message
from ..domain.profile import Profile
from ..domain.salience import FactSalience
from ..domain.space import Space
from ..domain.values import EntityRef, Predicate
from .queries import FactFilter, FactQuery

type SupportGroups = tuple[tuple[MessageId, ...], ...]


def flatten_support_groups(groups: SupportGroups) -> tuple[MessageId, ...]:
    """按首次出现顺序生成证据组的来源视图。

    Args:
        groups: 有序证据组。

    Returns:
        去重后的原文标识。
    """
    return tuple(dict.fromkeys(message_id for group in groups for message_id in group))


class SpaceUnavailable(LookupError):
    """表示空间不存在或当前账号无权访问。"""


class SessionUnavailable(LookupError):
    """表示对话在绑定空间内不可用。"""


class CommitOutcomeUnknown(RuntimeError):
    """表示写事务已进入提交阶段，但无法确认数据库是否已提交。"""


@dataclass(frozen=True)
class SessionRegistration:
    """空间中的会话登记，历史记录的创建时间可能未知。"""

    session_id: SessionId
    created_at: datetime | None


@dataclass(frozen=True)
class CutoffTrigger:
    """记录触发一次截止的事实及该次裁决采用的原文。

    Attributes:
        fact_id: 触发截止的事实标识。
        valid_end: 该触发事实设置的有效截止时刻，单位为 UTC epoch 秒。
        source_message_ids: 本次裁决采用的原文标识。裁决时无来源的手工事实使用空元组，
            旧审计无法还原逐项来源时为 None。
    """

    fact_id: FactId
    valid_end: float
    source_message_ids: tuple[MessageId, ...] | None = ()


def validate_cutoff_triggers(
    before: Fact | None,
    after: Fact,
    triggers: tuple[CutoffTrigger, ...],
    causes: Mapping[FactId, Fact],
    *,
    existing_causes: Mapping[FactId, Fact],
) -> None:
    """核对实际改变截止点的触发顺序及来源。

    Args:
        before: 保存前的事实，新事实为 None。
        after: 保存后的事实。
        triggers: 按实际裁决顺序排列的触发项。
        causes: 可引用的触发事实，以本批保存后的快照为准。
        existing_causes: 已有触发事实在本批保存前的快照。

    Raises:
        ValueError: 本次未设置或提前截止，或触发事实及来源无效。
    """
    if not triggers:
        return
    if after.invalid_at is None or triggers[-1].valid_end != after.invalid_at:
        raise ValueError("最后一个触发时点须等于事实的最终截止")
    previous_end = before.invalid_at if before is not None else None
    for trigger in triggers:
        if previous_end is not None and trigger.valid_end >= previous_end:
            raise ValueError("触发时点须依次设置或提前截止")
        cause = causes.get(trigger.fact_id)
        if cause is None or cause.id == after.id:
            raise ValueError("截止须引用绑定空间内另一条已存在或同批保存的事实")
        if trigger.valid_end != cause.valid_at:
            raise ValueError("触发时点须等于触发事实的生效点")
        sources = trigger.source_message_ids
        cause_before = existing_causes.get(cause.id, cause)
        if (
            sources is None
            or (not sources and cause.provenance and cause_before.provenance)
            or not set(sources).issubset(cause.provenance)
        ):
            raise ValueError("截止触发来源须是本次裁决采用的触发事实原文")
        previous_end = trigger.valid_end


@dataclass(frozen=True)
class FactChange:
    """记录一次事实创建、截止或元数据更新。

    Attributes:
        cursor: 存储按追加顺序分配的递增分页游标。
        changed_at: 写用例提供的修改时刻，单位为 UTC epoch 秒。
        before: 写入前的事实快照，创建时为 None。
        after: 写入后的事实快照。
        cutoff_triggers: 按裁决顺序记录实际设置或提前截止的触发项。
    """

    cursor: int
    changed_at: float
    before: Fact | None
    after: Fact
    cutoff_triggers: tuple[CutoffTrigger, ...] = ()


@dataclass(frozen=True)
class GraphEdge:
    """保存关系事实的标识和有方向的两个端点。

    Attributes:
        fact_id: 关系事实标识。
        subject_id: 主体实体标识。
        object_entity_id: 客体实体标识。
    """

    fact_id: FactId
    subject_id: EntityId
    object_entity_id: EntityId


@dataclass(frozen=True)
class GraphEdgeBatch:
    """保存一次邻接读取的结果。

    Attributes:
        edges: 按 FactId.value 升序排列的关系事实边。
        truncated: 本批还有符合条件且未返回的边。
    """

    edges: tuple[GraphEdge, ...]
    truncated: bool


class SpaceReader(Protocol):
    """读取绑定授权空间中的一致快照。

    会话在异步上下文内有效，由单个任务顺序使用。记录及其引用均限定在绑定空间内。
    摘要读取仅返回来源消息均存在且属于绑定空间和摘要所属会话的快照。

    混合检索的两路搜索和候选事实加载使用同一会话快照及请求时点。
    查询向量生成和候选重排在会话外完成。搜索以该快照中的事实记录为筛选依据，
    应用事实筛选条件和分数阈值后去重、排序及截取。已截止的事实仍可参与历史召回。
    """

    @property
    def space_id(self) -> SpaceId:
        """读取会话的空间作用域。

        Returns:
            已授权并绑定到会话的空间标识。
        """
        ...

    async def get_space(self) -> Space:
        """读取绑定空间的元数据。

        Returns:
            当前事务可见的空间快照。
        """
        ...

    async def get_erasure_token(self) -> str:
        """读取绑定空间在当前会话快照中的擦除标记。

        Returns:
            由存储为该空间分配的不透明标记。清空空间或实际删除会话原文时取新的
            未用值，其余写入期间保持稳定。
        """
        ...

    async def has_session(self, session_id: SessionId) -> bool:
        """判断对话是否在此快照的绑定空间内有效。

        Args:
            session_id: 后端签发的对话标识。

        Returns:
            绑定空间内有有效登记时为真。
        """
        ...

    async def get_session(self, session_id: SessionId) -> SessionRegistration | None:
        """读取绑定空间中的会话登记，查无记录时返回 None。"""
        ...

    async def list_sessions(
        self, *, after: SessionRegistration | None, limit: int
    ) -> Sequence[SessionRegistration]:
        """按创建时间倒序和标识升序分页，未知创建时间排在末尾。

        Args:
            after: 上一页末尾的排序位置，省略时读取首页。
            limit: 最大返回条数。

        Returns:
            本页会话标识，包括尚无原文的会话。
        """
        ...

    async def last_messages(
        self, session_id: SessionId, limit: int
    ) -> Sequence[Message]:
        """按追加顺序读取对话末尾的消息。

        排序使用存储分配的追加位置，recorded_at 记录收录时刻。

        Args:
            session_id: 要读取的对话标识。
            limit: 最大返回条数，须为正整数。

        Returns:
            最近追加的消息，从旧到新排列。
        """
        ...

    async def list_session_messages(
        self, session_id: SessionId, *, after: int | None, limit: int
    ) -> Sequence[tuple[int, Message]]:
        """按追加顺序分页读取会话原文及其排序位置。

        Args:
            session_id: 已校验存在的会话标识。
            after: 上一页末尾的排序位置，省略时读取首页。
            limit: 最大返回条数。

        Returns:
            追加位置和原文组成的序列，按追加位置升序排列。
        """
        ...

    async def get_messages(
        self, message_ids: Sequence[MessageId]
    ) -> Mapping[MessageId, Message]:
        """按标识批量读取绑定空间内的原文。

        仅返回存在的记录，键为消息标识；缺失的标识不出现在结果中。
        结果与输入顺序无关，调用方按需自行排序。

        Args:
            message_ids: 要读取的消息标识。

        Returns:
            以消息标识为键的已有原文映射。
        """
        ...

    async def facts_in_slot(
        self,
        subject: EntityRef,
        predicate: Predicate,
        *,
        valid_at: float | None = None,
        limit: int,
    ) -> Sequence[Fact]:
        """读取绑定空间中给定主体和谓词的事实。

        供有完整事实读取权限的调用方使用。

        Args:
            subject: 槽位的主体。
            predicate: 槽位的谓词。
            valid_at: 查询有效时点。为 None 时读取槽位时间线，包含已截止、
                未来生效及起点未知的事实。指定时点时，保留起点已知且有效区间
                覆盖该时点的事实。区间左闭右开，空截止表示开放上界。
            limit: 最大返回条数，须为正整数。

        Returns:
            按 recorded_at 倒序排列的事实。同一时刻按 FactId.value 升序排列。
        """
        ...

    async def get_fact(
        self, fact_id: FactId, *, exclude_sensitive: bool = False
    ) -> Fact | None:
        """按标识读取绑定空间中可见的事实。

        Args:
            fact_id: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。受限读取由服务端设为真。

        Returns:
            找到且可见的事实，缺失或被过滤时返回 None。
        """
        ...

    async def get_facts(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, Fact]:
        """按标识批量读取绑定空间内的事实。

        Args:
            ids: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。受限读取由服务端设为真。

        Returns:
            以事实标识为键的已找到且可见的快照。
        """
        ...

    async def get_relation_edges(
        self, fact_ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, GraphEdge]:
        """投影绑定空间内候选事实的关系端点。

        与按 ID 读取事实一致，有效时点由选出候选的查询处理。

        Args:
            fact_ids: 本快照中的候选事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            以事实标识为键的可见关系边。字面值事实和缺失事实省略。
        """
        ...

    async def get_salience(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, FactSalience]:
        """批量读取本会话快照中可见事实的显著度状态。

        Args:
            ids: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。受限读取由服务端设为真。

        Returns:
            以可见事实标识为键的状态。首次强化前返回默认状态，缺失或被筛掉的
            事实省略。当前上下文可用同一参考时刻计算衰减，给融合后的候选加权。
        """
        ...

    async def query_facts(
        self, spec: FactQuery, *, reference_time: float
    ) -> Sequence[Fact]:
        """从绑定空间分页读取符合条件的事实快照。

        Args:
            spec: 筛选条件、续页位置及条数上限。
            reference_time: 本次请求共用的 UTC epoch 秒。

        Returns:
            按 recorded_at 倒序、FactId.value 升序排列的事实，严格从游标
            之后开始，最多返回 spec.limit 条。
        """
        ...

    async def list_fact_changes(
        self,
        fact_id: FactId,
        *,
        after: int | None = None,
        limit: int = 100,
        exclude_sensitive: bool = False,
    ) -> Sequence[FactChange]:
        """按追加顺序倒序读取绑定空间内一条事实的修改记录。

        记录含完整快照，供有完整事实读取权限的调用方使用。

        Args:
            fact_id: 要读取修改记录的事实标识。
            after: 上一页最后一条记录的游标，仅返回游标更小的记录。
                省略时从最新一条开始。
            limit: 最大返回条数，须为正整数。
            exclude_sensitive: 在分页前隐藏敏感变更，并省略敏感触发引用。

        Returns:
            游标倒序的修改记录。无记录时返回空序列。
        """
        ...

    async def get_entity(self, entity_id: EntityId) -> Entity | None:
        """按标识读取绑定空间中的实体。

        Args:
            entity_id: 要读取的实体标识。

        Returns:
            实体快照，缺失时为 None。
        """
        ...

    async def get_entities(self, ids: Sequence[EntityId]) -> Mapping[EntityId, Entity]:
        """按标识批量读取绑定空间内的实体。

        受限读取路径使用已可见事实中的实体引用。

        Args:
            ids: 要读取的实体标识。

        Returns:
            以实体标识为键的已找到的快照。
        """
        ...

    async def edges(
        self,
        entity_id: EntityId,
        *,
        reference_time: float,
        valid_at: float | None = None,
        direction: Literal["in", "out", "both"] = "both",
        exclude_sensitive: bool = False,
        limit: int,
    ) -> Sequence[Fact]:
        """沿实体读取在查询时点有效的关系事实，自环只返回一次。

        仅返回 Fact.is_edge 为真的事实，并在排序截取前完成筛选。

        Args:
            entity_id: 绑定空间内要遍历的实体标识。
            reference_time: 本次请求共用的 UTC epoch 秒。
            valid_at: 显式有效时点，要求事实起点已知。省略时按
                reference_time 判断，并允许起点未知。
            direction: in 匹配客体，out 匹配主体，both 匹配任一端。
            exclude_sensitive: 是否排除敏感事实。受限读取由服务端设为真。
            limit: 最大返回条数，须为正整数。

        Returns:
            按有效起点倒序的边，未知起点排在最后，同值按 FactId.value 升序。
        """
        ...

    async def adjacent_edges(
        self,
        entity_ids: Sequence[EntityId],
        *,
        filters: FactFilter,
        reference_time: float,
        limit: int,
        exclude_fact_ids: Sequence[FactId] = (),
    ) -> GraphEdgeBatch:
        """在绑定空间的当前快照中批量读取实体关系边。

        主体或客体命中任一给定实体时返回该边，同一事实只返回一次。
        事实筛选与 query_facts 一致，筛选和排除均先于截取。

        Args:
            entity_ids: 用于匹配邻接边的实体标识。
            filters: 每条边必须满足的事实筛选条件。
            reference_time: 本次请求共用的 UTC epoch 秒。
            limit: 筛选及排除后最多返回的边数，须为正整数。
            exclude_fact_ids: 本次读取跳过的事实标识。
        Returns:
            按 FactId.value 升序排列的边及是否有符合条件但未返回的边。
            空端点返回未截断的空批次。
        """
        ...

    async def outgoing_edges(
        self,
        subject_ids: Sequence[EntityId],
        *,
        filters: FactFilter,
        reference_time: float,
        limit: int,
        predicates: Sequence[Predicate] = (),
    ) -> Sequence[GraphEdge]:
        """读取绑定空间内一层实体的有向出边。

        主体匹配后应用事实筛选和谓词条件，再执行批次限额。

        Args:
            subject_ids: 按遍历顺序排列的主体标识，重复项按首次位置处理。
            filters: 每条边必须满足的事实筛选条件。
            reference_time: 本次请求共用的 UTC epoch 秒。
            limit: 筛选后整批最多返回的边数，须为正整数。
            predicates: 边须匹配的规范谓词之一。空序列表示不限。

        Returns:
            按主体输入顺序、再按 FactId.value 升序排列的唯一关系边。
        """
        ...

    async def get_digest(self, digest_id: DigestId) -> Digest | None:
        """按标识读取绑定空间中可用的摘要。

        调用方须具有摘要所属会话的完整读取权限。

        Args:
            digest_id: 要读取的摘要标识。

        Returns:
            摘要及其来源均可用时返回快照，否则返回 None。
        """
        ...

    async def get_digest_for_group(
        self, session_id: SessionId, source_message_ids: Sequence[MessageId]
    ) -> Digest | None:
        """按固定消息组读取可用的摘要。

        调用方须具有该会话的完整读取权限。

        组身份 = 会话标识 + 去重后保持首次出现顺序的消息标识序列，与 Digest 构造一致。

        Args:
            session_id: 摘要所属的会话。
            source_message_ids: 组成摘要的消息标识序列。

        Returns:
            对应消息组的可用摘要快照。同组有多份时按 updated_at 倒序，再按
            DigestId.value 升序选取。无可用摘要时返回 None。
        """
        ...

    async def get_latest_digest(self, session_id: SessionId) -> Digest | None:
        """读取绑定空间内覆盖消息位置最靠后的会话摘要。

        调用方须具有该会话的完整读取权限。

        消息位置使用存储分配的追加序，recorded_at 记录收录时刻。

        Args:
            session_id: 摘要所属的会话标识。

        Returns:
            按来源消息最晚追加位置选出的可用摘要。同一位置按 updated_at
            倒序，再按 DigestId.value 升序选取。无可用摘要时返回 None。
        """
        ...

    async def get_profile(self, *, exclude_sensitive: bool = False) -> Profile | None:
        """读取绑定空间的画像快照。

        Args:
            exclude_sensitive: 受限读取由服务端设为真，此时省略整份画像。

        Returns:
            完整读取权限下的画像快照，缺失或受限时返回 None。
        """
        ...

    async def search_vectors(
        self,
        vector: Sequence[float],
        limit: int,
        *,
        filters: FactFilter,
        reference_time: float,
        embedding_space: str,
        min_score: float | None = None,
    ) -> Sequence[tuple[FactId, float]]:
        """在当前会话快照中按向量相似性搜索事实候选。

        Args:
            vector: 查询向量，长度须与嵌入空间绑定的维度一致。
            limit: 最大返回条数，须为正整数。
            filters: 按有效时间、可见性等条件筛选事实。
            reference_time: 本次请求共用的 UTC epoch 秒。
            embedding_space: 与写入一致的向量模型及版本标识。
            min_score: 向量通道的最低分数，包含阈值本身。省略时不限。

        Returns:
            去重后的候选事实标识及有限分数。分数从高到低排列，
            同分按 FactId.value 升序排列。
        """
        ...

    async def search_lexical(
        self,
        query: str,
        limit: int,
        *,
        filters: FactFilter,
        reference_time: float,
        min_score: float | None = None,
    ) -> Sequence[tuple[FactId, float]]:
        """在当前会话快照中基于 Fact.text 搜索词法候选。

        Args:
            query: 用于匹配事实正文的查询文本。
            limit: 最大返回条数，须为正整数。
            filters: 按有效时间、可见性等条件筛选事实。
            reference_time: 本次请求共用的 UTC epoch 秒。
            min_score: 词法通道的最低分数，包含阈值本身。省略时不限。

        Returns:
            去重后的候选事实标识及有限分数。分数从高到低排列，
            同分按 FactId.value 升序排列。
        """
        ...


class SpaceSession(SpaceReader, Protocol):
    """在绑定空间的写会话中执行事务操作。

    写操作要求以 write=True 打开会话。派生写入的调用方在准备输入的读会话内
    读取擦除标记，再在事务外生成。发布前进入串行写会话，核对擦除标记并重读
    参与决策的来源、目标事实和槽位。标记或依赖发生变化时放弃整批派生写入。
    """

    async def register_session(self, session_id: SessionId) -> SessionRegistration:
        """登记由应用生成的新对话标识。

        Args:
            session_id: 后端新生成的标识。
        Returns:
            已登记的会话及创建时间。

        Raises:
            ValueError: 绑定空间内已有同一标识。
        """
        ...

    async def append_messages(self, messages: Sequence[Message]) -> None:
        """按输入顺序追加新消息。

        调用方为同一消息的重试复用 MessageId。同 ID 的空间、会话、角色、
        正文和事件时刻一致时，沿用首次收录的 recorded_at 和原追加位置。

        Args:
            messages: 待追加或重放的消息。

        Raises:
            SessionUnavailable: 任一原文的对话在绑定空间内不可用。
            ValueError: 同 ID 的消息空间、会话、角色、正文或事件时刻冲突，
                整批拒绝。
        """
        ...

    async def get_fact_support_groups(
        self, fact_ids: Sequence[FactId]
    ) -> Mapping[FactId, SupportGroups]:
        """批量读取写会话中事实的完整证据组。

        Args:
            fact_ids: 绑定空间内待查询的事实标识。

        Returns:
            事实到有序证据组的映射。无来源的事实对应空元组，缺失事实省略。
        """
        ...

    async def save_facts(
        self,
        facts: Sequence[Fact],
        *,
        changed_at: float,
        support_groups: Mapping[FactId, SupportGroups] | None = None,
        cutoff_triggers: Mapping[FactId, tuple[CutoffTrigger, ...]] | None = None,
    ) -> tuple[FactChange, ...]:
        """按标识创建事实或更新截止与元数据。

        同 ID 可更新 labels、sensitive、provenance。首次截止同时设置
        invalid_at 和 expired_at，提前截止保留首次获知的 expired_at。
        其余字段固定，包括 subject、predicate、object、text、valid_at、
        recorded_at 和 source。主体、实体客体及已提供的来源原文须在
        绑定空间的当前写事务内存在。新建手工事实的 provenance 须为空，
        后续可追加会话证据组。

        每次创建或实际更新在同一事务追加一条 FactChange。创建事实时由存储
        从 Fact.text 派生词法索引，与事实一同提交。事实和分组相同的重放幂等。
        替代时以同一时点截止旧事实和启用新 ID 的事实，有向量时在同一写会话
        写入新向量。旧事实保留正文和索引，供按有效时间查询。

        Args:
            facts: 待保存的事实快照，同批每个标识一份。
            changed_at: 调用方从 Clock 取得的本次变更时刻，单位为 UTC epoch 秒。
                在线创建可与 recorded_at 共用一次时钟读取，历史导入可保留原 recorded_at。
            support_groups: 事实写入后的完整证据组。每组需要全部成员，组之间独立。
                展开去重后的顺序须与 provenance 一致。改变已有来源时须提供；
                新事实省略时，所有来源组成一组。
            cutoff_triggers: 按裁决顺序记录的触发项，截止点严格递减并以最终截止收尾。
                每项截止点等于触发事实的生效点，来源须取自触发事实。

        Returns:
            本次实际写入的变更，按游标升序排列。空批次和幂等重放返回空元组。

        Raises:
            ValueError: 同批标识重复、事实超出绑定空间、新建手工事实引用原文、
                修改固定字段、重开或延后截止，或证据组及触发引用违反契约。整批拒绝。
        """
        ...

    async def save_salience(self, states: Mapping[FactId, FactSalience]) -> None:
        """在同一写会话中批量替换事实的显著度状态。

        强化用当前写会话读到的最新显著度计算。此处仅更新访问强化状态，
        该写入独立于事实修改记录和画像。

        Args:
            states: 以绑定空间内已存在的事实标识为键的状态。

        Raises:
            ValueError: 存在不属于绑定空间或已缺失的事实标识时，整批拒绝。
        """
        ...

    async def delete_facts(self, fact_ids: Sequence[FactId]) -> None:
        """幂等删除事实及其显著度、索引项、修改记录与关联引用。

        清理引用被删触发事实的变更记录及依赖被删事实的画像。

        Args:
            fact_ids: 绑定空间内待删除的事实标识。
        """
        ...

    async def delete_session_data(self, session_id: SessionId) -> frozenset[FactId]:
        """幂等撤销对话登记并擦除其原文、摘要和证据贡献。

        受影响的证据组整体撤下。抽取事实失去全部证据组时删除，
        包括已截止和未来生效的事实。手工事实及仍有完整证据组的事实保留有效区间。
        删除引用已擦除来源的审计记录。实际删除原文时更新擦除标记。

        Args:
            session_id: 绑定空间内待清理的会话标识。

        Returns:
            撤下的事实标识，依赖这些事实的画像已在同一事务撤下。
        """
        ...

    async def purge_space(self) -> None:
        """清空对话登记、原文、实体、事实、显著度、索引、摘要、画像和修改记录。

        保留空间和各嵌入模型已登记的维度。清空数据和更换擦除标记在同一
        事务中完成。空空间清空时也更换标记。
        """
        ...

    async def upsert_entity(self, entity: Entity) -> Entity:
        """保存实体快照。

        Args:
            entity: 待保存的实体。

        Returns:
            存储最终保存的实体快照。

        Raises:
            ValueError: 实体超出绑定空间。
        """
        ...

    async def save_digest(self, digest: Digest) -> None:
        """按摘要标识保存整份摘要快照。

        适配器核对来源消息均属于绑定空间和摘要所属会话。
        同 ID 的会话、来源顺序和创建时刻固定，更新时间可以持平或前进。

        Args:
            digest: 待保存的摘要。

        Raises:
            ValueError: 摘要或来源超出所属空间或会话，来源缺失，修改同 ID
                的消息组或创建时刻，或更新时间倒退。
        """
        ...

    async def save_profile(self, profile: Profile) -> None:
        """保存绑定空间的整份画像快照，来源事实须在该空间内存在。

        已存在画像时，更新时间可以持平或前进。

        Args:
            profile: 待保存的画像。

        Raises:
            ValueError: 画像超出绑定空间、来源事实缺失或更新时间倒退。
        """
        ...

    async def delete_profile(self) -> None:
        """幂等删除绑定空间的画像，随后 get_profile 返回 None。

        当画像引用的事实被删除或其有效期、标签、敏感性发生变化时，调用方
        在处理事实的同一写会话内调用本方法。单独追加证据组可沿用现有画像。
        """
        ...

    async def upsert_vectors(
        self, items: Sequence[tuple[FactId, Sequence[float]]], *, embedding_space: str
    ) -> None:
        """在当前会话事务中按事实标识写入或替换向量索引项。

        事实记录与向量索引随同一事务提交或回滚。

        Args:
            items: 绑定空间内的事实标识和向量。
            embedding_space: 向量模型及版本标识，与查询一致。适配器为每个
                嵌入空间绑定唯一向量维度。
        """
        ...


class Storage(Protocol):
    """定义账号级空间创建和空间事务入口。

    首个适配器将事实、修改记录和两种索引放在同一 PostgreSQL 数据库，
    并在同一写事务内发布。
    """

    async def create_space(self, space: Space) -> None:
        """创建归属可信账号的空间。

        适配器根据认证上下文校验 space.owner_id。

        Args:
            space: 待创建的空间快照。

        Raises:
            PermissionError: 空间所有者与认证账号不一致。
            ValueError: 空间标识已存在。
        """
        ...

    @overload
    def session(
        self, space_id: SpaceId, *, write: Literal[True]
    ) -> AbstractAsyncContextManager[SpaceSession]: ...

    @overload
    def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AbstractAsyncContextManager[SpaceReader]: ...

    def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AbstractAsyncContextManager[SpaceReader]:
        """返回管理空间事务的异步上下文管理器。

        进入时验证空间存在且已授权，并开启事务。只读会话提供一致快照。
        写会话取得本空间的串行写入权后读取最新已提交的标记和依赖。
        写会话正常退出时提交，块内异常或取消时回滚并传播异常。
        若捕获数据库异常后写事务仍处于失败状态，会话退出时抛出 RuntimeError
        并回滚。
        PostgreSQL 适配器为每次只读会话开启一个 REPEATABLE READ 事务。

        Args:
            space_id: 要访问的空间标识。
            write: 为 True 时开启写会话，否则开启只读会话。

        Returns:
            显式传入 write=True 时类型为 SpaceSession，其余调用按 SpaceReader 使用。

        Raises:
            LookupError: 进入会话时空间不存在或账号无权访问。
            RuntimeError: 退出写会话时，事务因已捕获的数据库错误而无法提交。
            CommitOutcomeUnknown: 写事务进入提交阶段后无法确认结果。
        """
        ...
