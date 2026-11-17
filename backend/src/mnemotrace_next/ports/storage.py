"""定义空间存储及事务会话端口。"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
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


@dataclass(frozen=True)
class FactChange:
    """记录一次事实创建、截止或元数据更新。

    Attributes:
        cursor: 存储按追加顺序分配的递增分页游标。
        changed_at: 写用例提供的修改时刻，单位为 UTC epoch 秒。
        before: 写入前的事实快照，创建时为 None。
        after: 写入后的事实快照。
    """

    cursor: int
    changed_at: float
    before: Fact | None
    after: Fact


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
        """返回会话绑定的空间标识。"""
        ...

    async def get_space(self) -> Space:
        """返回绑定空间的快照。"""
        ...

    async def get_purge_token(self) -> str:
        """读取绑定空间在当前会话快照中的清空标记。

        Returns:
            由存储为该空间分配的标记，每次清空取新的未用值，普通写入期间保持稳定。
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
        self, fact_id: FactId, *, after: int | None = None, limit: int = 100
    ) -> Sequence[FactChange]:
        """按追加顺序倒序读取绑定空间内一条事实的修改记录。

        记录含完整快照，供有完整事实读取权限的调用方使用。

        Args:
            fact_id: 要读取修改记录的事实标识。
            after: 上一页最后一条记录的游标，仅返回游标更小的记录。
                省略时从最新一条开始。
            limit: 最大返回条数，须为正整数。

        Returns:
            游标倒序的修改记录。无记录时返回空序列。
        """
        ...

    async def get_entity(self, entity_id: EntityId) -> Entity | None:
        """按标识读取绑定空间中的实体，缺失时返回 None。

        Args:
            entity_id: 要读取的实体标识。
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
            对应消息组的可用摘要快照，无可用摘要时返回 None。
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
    读取清空标记，再在事务外生成。发布前进入串行写会话，核对清空标记并重读
    参与决策的来源、目标事实和槽位。标记或依赖发生变化时放弃整批派生写入。
    """

    async def append_messages(self, messages: Sequence[Message]) -> None:
        """按输入顺序追加新消息。

        调用方为同一消息的重试复用 MessageId。同 ID 的空间、会话、角色、
        正文和事件时刻一致时，沿用首次收录的 recorded_at 和原追加位置。

        Args:
            messages: 待追加或重放的消息。

        Raises:
            ValueError: 同 ID 的消息空间、会话、角色、正文或事件时刻冲突，
                整批拒绝。
        """
        ...

    async def save_facts(self, facts: Sequence[Fact], *, changed_at: float) -> None:
        """按标识创建事实或更新截止与元数据。

        同 ID 可更新 labels、sensitive、provenance。首次截止同时设置
        invalid_at 和 expired_at，提前截止保留首次获知的 expired_at。
        其余字段固定，包括 subject、predicate、object、text、valid_at、
        recorded_at、source 和 supersedes。新事实的 supersedes 指向本批
        保存前绑定空间内已存在的事实。

        每次创建或实际更新在同一事务追加一条 FactChange。创建事实时由存储
        从 Fact.text 派生词法索引，与事实一同提交。相同快照重放幂等。
        替代时以同一时点截止旧事实和启用新 ID 的事实，有向量时在同一写会话
        写入新向量。旧事实保留正文和索引，供按有效时间查询。

        Args:
            facts: 待保存的事实快照，同批每个标识一份。
            changed_at: 调用方从 Clock 取得的本次变更时刻，单位为 UTC epoch 秒。
                在线创建可与 recorded_at 共用一次时钟读取，历史导入可保留原 recorded_at。

        Raises:
            ValueError: 事实超出绑定空间、修改固定字段、重开或延后截止、
                修改首次获知时刻，或新事实引用尚不存在的前驱。整批拒绝。
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

        从其余事实的前驱引用中移除被删除的标识。

        Args:
            fact_ids: 绑定空间内待删除的事实标识。
        """
        ...

    async def delete_session_data(self, session_id: SessionId) -> frozenset[FactId]:
        """幂等删除会话原文、摘要和引用这些原文的全部已保存事实。

        删除范围包括已截止和未来生效的事实。
        整条事实及其索引、修改记录和关联引用按 delete_facts 的规则撤下。
        对仍存在的事实，创建或修改记录的任一快照引用被删原文时，删除该记录。
        有剩余来源时，调用方可重新抽取并按正常写入流程发布。

        Args:
            session_id: 绑定空间内待清理的会话标识。

        Returns:
            撤下的事实标识，供调用方在同一事务内判断是否撤下画像。
        """
        ...

    async def purge_space(self) -> None:
        """清空原文、实体、事实及其显著度、索引、摘要、画像和修改记录。

        保留空间。清空数据和更换清空标记在同一事务中完成。空空间清空时也更换标记。
        """
        ...

    async def upsert_entity(self, entity: Entity) -> Entity:
        """保存实体并返回最终保存的快照。

        Args:
            entity: 待保存的实体。
        """
        ...

    async def save_digest(self, digest: Digest) -> None:
        """按摘要标识保存整份摘要快照。

        适配器核对来源消息均属于绑定空间和摘要所属会话。

        Args:
            digest: 待保存的摘要。
        """
        ...

    async def save_profile(self, profile: Profile) -> None:
        """保存绑定空间的整份画像快照。

        Args:
            profile: 待保存的画像。
        """
        ...

    async def delete_profile(self) -> None:
        """幂等删除绑定空间的画像，随后 get_profile 返回 None。

        当实际变更或删除的事实出现在 Profile.source_fact_ids 中时，调用方
        在处理事实的同一个写会话内调用本方法。
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
        PostgreSQL 适配器为每次只读会话开启一个 REPEATABLE READ 事务。

        Args:
            space_id: 要访问的空间标识。
            write: 为 True 时开启写会话，否则开启只读会话。

        Returns:
            显式传入 write=True 时类型为 SpaceSession，其余调用按 SpaceReader 使用。
        """
        ...
