"""编排一次同步记忆写入及提交前复核。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, tzinfo
from typing import Literal

from ..domain.enums import Role
from ..domain.errors import InvalidMessage
from ..domain.ids import MessageId, SessionId, SpaceId
from ..domain.message import Message
from ..ports.inference import LLM, Embedder
from ..ports.reranker import Reranker
from ..ports.runtime import Clock, IdGenerator
from ..ports.storage import (
    FactChange,
    SessionUnavailable,
    SpaceReader,
    SpaceSession,
    Storage,
)
from .adjudication import (
    BatchPairs,
    ComparisonCandidates,
    FactComparison,
    PreparedFact,
    build_batch_pairs,
    compare_facts,
    find_comparison_candidates,
    prepare_facts,
    resolve_known_comparisons,
)
from .extraction import extract_facts, message_reference_time
from .fact_lifecycle import calculate_fact_writes
from .recall import DEFAULT_TOP_K, Recall, RecallResult
from .sessions import create_session

_CONTEXT_MESSAGES = 10
_COMPARISON_RETRIES = 1


@dataclass(frozen=True)
class MessageInput:
    """接收批次中的一条原文，event_time 是以 UTC epoch 秒表示的发言时刻。"""

    role: Literal["user", "assistant"]
    content: str
    event_time: float | None = None


@dataclass(frozen=True)
class RememberResult:
    """保存已提交的原文标识与实际事实变更。

    Attributes:
        message_ids: 按输入顺序排列的原文标识。
        fact_changes: 按审计游标升序排列的事实变更。
    """

    message_ids: tuple[MessageId, ...]
    fact_changes: tuple[FactChange, ...]


class RememberInvalidated(RuntimeError):
    """表示空间发生擦除，本次请求使用的快照已经失效。"""


class RememberRetryRequired(RuntimeError):
    """表示已用完重判机会，且本次提交尚未开始写入。"""


@dataclass(frozen=True)
class _RememberContext:
    """保存本次使用的已提交前文、新原文和提交前须复核的擦除标记。"""

    received_at: float
    erasure_token: str
    previous: tuple[Message, ...]
    messages: tuple[Message, ...]


class Memory:
    """为服务端选定的空间提供会话登记、记忆写入与召回。

    Args:
        space_id: 服务端选定的目标空间，存储会话仍会校验访问权。
        storage: 已绑定认证账号的存储端口。
        llm: 装配时已校验配置的结构化补全端口。
        embedder: 装配时已校验配置的嵌入端口。
        clock: 提供 UTC epoch 秒的时钟。
        ids: 提供不可复用标识的生成器。
        timezone: 解释日历日期的时区，默认 UTC。
        exclude_sensitive: 是否排除召回中的敏感事实，由服务端设置。
        reranker: 由服务端装配且获准接收候选正文的可选重排端口。

    Raises:
        ValueError: 缺少补全或嵌入端口。
    """

    def __init__(
        self,
        *,
        space_id: SpaceId,
        storage: Storage,
        llm: LLM,
        embedder: Embedder,
        clock: Clock,
        ids: IdGenerator,
        timezone: tzinfo = UTC,
        exclude_sensitive: bool = False,
        reranker: Reranker | None = None,
    ) -> None:
        if llm is None:
            raise ValueError("Memory 需要配置补全模型")
        self._recall = Recall(
            space_id=space_id,
            storage=storage,
            embedder=embedder,
            clock=clock,
            exclude_sensitive=exclude_sensitive,
            reranker=reranker,
        )
        self._space_id = space_id
        self._storage = storage
        self._llm = llm
        self._embedder = embedder
        self._clock = clock
        self._ids = ids
        self._timezone = timezone

    @staticmethod
    def for_recall(
        *,
        space_id: SpaceId,
        storage: Storage,
        embedder: Embedder,
        clock: Clock,
        exclude_sensitive: bool = False,
        reranker: Reranker | None = None,
    ) -> Recall:
        """只装配召回所需的依赖。

        Args:
            space_id: 服务端选定且由存储授权的空间。
            storage: 已绑定认证账号的存储端口。
            embedder: 查询使用的嵌入端口。
            clock: 提供 UTC epoch 秒的时钟。
            exclude_sensitive: 是否排除敏感事实，由服务端设置。
            reranker: 由服务端装配且获准接收候选正文的可选重排端口。

        Returns:
            与读写装配使用相同接口的只读召回对象。
        """
        return Recall(
            space_id=space_id,
            storage=storage,
            embedder=embedder,
            clock=clock,
            exclude_sensitive=exclude_sensitive,
            reranker=reranker,
        )

    async def recall(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        as_of: float | None = None,
        rerank: bool = False,
    ) -> RecallResult:
        """在本空间召回可见的事实证据。

        Args:
            query: 非空查询。
            top_k: 最多返回的证据条数。
            as_of: 显式有效时点，以 UTC epoch 秒表示。
            rerank: 是否调用已装配的重排器调整候选顺序。

        Returns:
            按相关性排列的事实证据及可选的超时降级原因。
        """
        return await self._recall.recall(query, top_k=top_k, as_of=as_of, rerank=rerank)

    async def create_session(self) -> SessionId:
        """登记一个新会话，提交后返回标识。

        Returns:
            可用于本空间记忆写入的会话标识。

        Raises:
            LookupError: 无权访问绑定空间。
        """
        return await create_session(self._storage, self._space_id, self._ids)

    async def remember(
        self, messages: str | Sequence[MessageInput], *, session_id: SessionId
    ) -> RememberResult:
        """在事务外生成事实，在空间写锁保护下复核并发布。

        Args:
            messages: 单条用户文本，或非空的有序消息批次。
            session_id: 绑定空间内已登记的会话标识。

        Returns:
            PostgreSQL 确认提交后的原文标识和事实变更。

        Raises:
            InvalidMessage: 消息批次或内容无效。
            LookupError: 无权访问绑定空间。
            SessionUnavailable: 会话不可用。
            RememberInvalidated: 空间擦除使本次快照失效。
            RememberRetryRequired: 重判后再次遇到需要模型裁决的变化。
            CommitOutcomeUnknown: 写事务进入提交阶段后无法确认结果。
        """
        # 固定本批原文和抽取所需的前文，保留擦除标记供提交前复核。
        context = await _prepare_remember_context(
            self._storage,
            self._space_id,
            session_id,
            messages,
            clock=self._clock,
            ids=self._ids,
            timezone=self._timezone,
        )

        # 在写锁外抽取事实、生成向量并完成首次裁决。
        extracted = await extract_facts(
            self._llm, context.previous, context.messages, timezone=self._timezone
        )
        if extracted:
            async with self._storage.session(self._space_id) as reader:
                await _check_remember_snapshot(
                    reader, session_id, context.erasure_token
                )
        prepared = await prepare_facts(
            extracted,
            space_id=self._space_id,
            embedder=self._embedder,
            ids=self._ids,
            received_at=context.received_at,
        )
        batch_pairs = build_batch_pairs(prepared)
        candidates = ComparisonCandidates({}, ())
        if prepared:
            async with self._storage.session(self._space_id) as reader:
                await _check_remember_snapshot(
                    reader, session_id, context.erasure_token
                )
                candidates = await self._find_candidates(
                    reader, prepared, batch_pairs, context.received_at
                )
        comparisons = await compare_facts(self._llm, prepared, candidates)

        # 持锁复核会话与候选。当前配对的裁决齐全时，原文和事实变更一起提交。
        for attempt in range(_COMPARISON_RETRIES + 1):
            async with self._storage.session(self._space_id, write=True) as writer:
                await _check_remember_snapshot(
                    writer, session_id, context.erasure_token
                )
                current = await self._find_candidates(
                    writer, prepared, batch_pairs, context.received_at
                )
                resolved = resolve_known_comparisons(
                    prepared, current, candidates, comparisons
                )
                if len(resolved) == len(current.pairs):
                    changes = await self._publish(
                        writer,
                        context,
                        prepared,
                        current,
                        tuple(resolved[pair] for pair in current.pairs),
                    )
                    return RememberResult(
                        tuple(message.id for message in context.messages), changes
                    )

            # 锁外只补判缺项，避免推理阻塞同一空间的其他写入。
            # 用完重判机会后，由调用方决定是否重新发起写入。
            if attempt == _COMPARISON_RETRIES:
                break
            candidates = current
            async with self._storage.session(self._space_id) as reader:
                await _check_remember_snapshot(
                    reader, session_id, context.erasure_token
                )
            comparisons = await compare_facts(
                self._llm, prepared, current, reusable=resolved
            )

        raise RememberRetryRequired("比较依据再次变化，请重新发起写入")

    async def _find_candidates(
        self,
        reader: SpaceReader,
        prepared: Sequence[PreparedFact],
        batch_pairs: BatchPairs,
        reference_time: float,
    ) -> ComparisonCandidates:
        if not prepared:
            return ComparisonCandidates({}, ())
        return await find_comparison_candidates(
            reader,
            prepared,
            embedding_space=self._embedder.embedding_space,
            reference_time=reference_time,
            batch_pairs=batch_pairs,
        )

    async def _publish(
        self,
        writer: SpaceSession,
        context: _RememberContext,
        prepared: Sequence[PreparedFact],
        candidates: ComparisonCandidates,
        comparisons: Sequence[FactComparison],
    ) -> tuple[FactChange, ...]:
        if not prepared:
            await writer.append_messages(context.messages)
            return ()

        # 读取重复或矛盾裁决涉及的旧事实证据组，用锁内的最新证据计算变更。
        existing = candidates.existing_facts
        evidence_ids = tuple(
            dict.fromkeys(
                fact_id
                for comparison in comparisons
                if comparison.verdict in ("duplicate", "conflict")
                for fact_id in comparison.pair
                if fact_id in existing
            )
        )
        groups = (
            await writer.get_fact_support_groups(evidence_ids) if evidence_ids else {}
        )
        if groups.keys() != set(evidence_ids):
            raise RuntimeError("写会话中旧候选的证据组读取不完整")
        changed_at = max(
            self._clock.now(),
            context.received_at,
            max(
                (fact.recorded_at for fact in existing.values()),
                default=context.received_at,
            ),
        )
        writes = calculate_fact_writes(
            prepared,
            comparisons,
            existing_facts=existing,
            existing_support_groups=groups,
            changed_at=changed_at,
        )

        # 原文和实际产生的事实变更在同一事务内提交，零变更也保留原文。
        await writer.append_messages(context.messages)
        if not writes.facts:
            return ()
        if writes.entities:
            existing_entities = await writer.get_entities(
                tuple(entity.id for entity in writes.entities)
            )
            for entity in writes.entities:
                if entity.id not in existing_entities:
                    await writer.upsert_entity(entity)
        changes = await writer.save_facts(
            writes.facts,
            changed_at=changed_at,
            support_groups=writes.support_groups,
            cutoff_triggers=writes.cutoff_triggers,
        )
        if writes.vectors:
            await writer.upsert_vectors(
                tuple(writes.vectors.items()),
                embedding_space=self._embedder.embedding_space,
            )

        # 引用事实的截止时间、标签或敏感标记变化时，清理基于旧状态派生的画像。
        dirty_profile_sources = {
            change.after.id
            for change in changes
            if change.before is not None
            and (
                change.before.invalid_at != change.after.invalid_at
                or change.before.labels != change.after.labels
                or change.before.sensitive != change.after.sensitive
            )
        }
        if dirty_profile_sources:
            profile = await writer.get_profile()
            if profile is not None and dirty_profile_sources.intersection(
                profile.source_fact_ids
            ):
                await writer.delete_profile()
        return changes


async def _check_remember_snapshot(
    reader: SpaceReader, session_id: SessionId, erasure_token: str
) -> None:
    if not await reader.has_session(session_id):
        raise SessionUnavailable("会话不可用")
    if await reader.get_erasure_token() != erasure_token:
        raise RememberInvalidated("空间发生擦除，本次写入已失效")


async def _prepare_remember_context(
    storage: Storage,
    space_id: SpaceId,
    session_id: SessionId,
    messages: str | Sequence[MessageInput],
    *,
    clock: Clock,
    ids: IdGenerator,
    timezone: tzinfo = UTC,
) -> _RememberContext:
    """校验新消息，在一个只读快照里授权并读取抽取前文。

    Args:
        storage: 已绑定认证账号的存储端口。
        space_id: 目标空间。
        session_id: 已登记的会话。
        messages: 单条用户文本或按提交顺序排列的消息批次。
        clock: 提供本次请求接收时刻。
        ids: 提供本批尚未入库的原文标识。
        timezone: 抽取解释日历日期所用的时区。

    Returns:
        供后续抽取和提交复核复用的输入。

    Raises:
        InvalidMessage: 批次或其中的消息无效。
        LookupError: 无权访问空间，或会话在该空间内不可用。
    """
    received_at = clock.now()
    inputs = (
        (MessageInput("user", messages),) if isinstance(messages, str) else messages
    )
    if not isinstance(inputs, Sequence) or not inputs:
        raise InvalidMessage("消息批次须为非空的有序序列")

    prepared = tuple(
        _prepare_message(item, space_id, session_id, received_at, ids)
        for item in inputs
    )
    if any(
        message_reference_time(message.event_time, timezone=timezone) is None
        for message in prepared
    ):
        raise InvalidMessage("消息发言时刻超出支持的日历范围")
    async with storage.session(space_id) as reader:
        if not await reader.has_session(session_id):
            raise SessionUnavailable("会话不可用")
        erasure_token = await reader.get_erasure_token()
        previous = tuple(await reader.last_messages(session_id, _CONTEXT_MESSAGES))
    return _RememberContext(received_at, erasure_token, previous, prepared)


def _prepare_message(
    item: MessageInput,
    space_id: SpaceId,
    session_id: SessionId,
    received_at: float,
    ids: IdGenerator,
) -> Message:
    if not isinstance(item, MessageInput):
        raise InvalidMessage("批次成员须为 MessageInput")
    try:
        role = Role(item.role)
    except ValueError:
        raise InvalidMessage("role 须为 user 或 assistant") from None
    message = Message(
        id=ids.new_message_id(),
        space_id=space_id,
        session_id=session_id,
        role=role,
        content=item.content,
        event_time=received_at if item.event_time is None else item.event_time,
        recorded_at=received_at,
    )
    try:
        message.content.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidMessage("原文须能编码为 UTF-8") from None
    if "\x00" in message.content:
        raise InvalidMessage("原文不能含空字符")
    return message
