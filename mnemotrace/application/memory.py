"""装配空间的会话、记忆写入和召回用例。"""

from collections.abc import Sequence
from datetime import UTC, tzinfo

from ..domain.ids import SessionId, SpaceId
from ..ports.inference import LLM, Embedder
from ..ports.reranker import Reranker
from ..ports.runtime import Clock, IdGenerator
from ..ports.storage import Storage
from .recall import DEFAULT_VECTOR_MIN_COSINE, Recall, RecallQuery, RecallResult
from .remember import MessageInput, Remember, RememberResult
from .sessions import create_session


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
        vector_min_cosine: 向量候选的最低余弦分数，包含阈值本身。

    Raises:
        ValueError: 缺少补全或嵌入端口、召回可见性选项无效或余弦阈值越界。
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
        vector_min_cosine: float = DEFAULT_VECTOR_MIN_COSINE,
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
            vector_min_cosine=vector_min_cosine,
        )
        self._remember = Remember(
            space_id=space_id,
            storage=storage,
            llm=llm,
            embedder=embedder,
            clock=clock,
            ids=ids,
            timezone=timezone,
        )
        self._space_id = space_id
        self._storage = storage
        self._ids = ids

    @staticmethod
    def for_recall(
        *,
        space_id: SpaceId,
        storage: Storage,
        embedder: Embedder,
        clock: Clock,
        exclude_sensitive: bool = False,
        reranker: Reranker | None = None,
        vector_min_cosine: float = DEFAULT_VECTOR_MIN_COSINE,
    ) -> Recall:
        """只装配召回所需的依赖。

        Args:
            space_id: 服务端选定且由存储授权的空间。
            storage: 已绑定认证账号的存储端口。
            embedder: 查询使用的嵌入端口。
            clock: 提供 UTC epoch 秒的时钟。
            exclude_sensitive: 是否排除敏感事实，由服务端设置。
            reranker: 由服务端装配且获准接收候选正文的可选重排端口。
            vector_min_cosine: 向量候选的最低余弦分数，包含阈值本身。

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
            vector_min_cosine=vector_min_cosine,
        )

    async def recall(self, query: RecallQuery) -> RecallResult:
        """在本空间召回可见的事实证据。

        Args:
            query: 已校验的召回查询及选项。

        Returns:
            按相关性排列的事实证据及可选的超时降级原因。
        """
        return await self._recall.recall(query)

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
        """同步抽取并写入本次记忆。

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
            RememberRetryRequired: 锁外重查后，事实版本在发布前再次变化。
            CommitOutcomeUnknown: 写事务进入提交阶段后无法确认结果。
        """
        return await self._remember.remember(messages, session_id=session_id)
