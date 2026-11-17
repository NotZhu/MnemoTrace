"""强化实际进入当前上下文的事实。"""

from collections.abc import Sequence

from ..domain.ids import FactId, SpaceId
from ..ports.runtime import Clock
from ..ports.storage import Storage


async def reinforce_context_facts(
    storage: Storage,
    clock: Clock,
    space_id: SpaceId,
    fact_ids: Sequence[FactId],
    *,
    reference_time: float,
    expected_purge_token: str,
    exclude_sensitive: bool = False,
) -> None:
    """为实际进入当前上下文且在请求时点有效的事实写回显著度。

    调用方仅在显式开启强化，且查询使用 state="current"、valid_at=None 时调用。
    写会话按请求时点复核最新事实的有效区间。每个事实只强化一次，发生空间清空时
    放弃本次写回。

    Args:
        storage: 空间事务端口。
        clock: 写会话内获取强化时间的时钟。
        space_id: 上下文所属空间。
        fact_ids: 最终进入上下文的事实标识。
        reference_time: 组装上下文时选取当前事实所用的 UTC epoch 秒。
        expected_purge_token: 读取上下文时在同一快照中取得的清空标记。
        exclude_sensitive: 是否排除敏感事实，应与读取上下文的权限一致。
    """
    if not fact_ids:
        return

    async with storage.session(space_id, write=True) as session:
        if await session.get_purge_token() != expected_purge_token:
            return

        facts = await session.get_facts(fact_ids, exclude_sensitive=exclude_sensitive)
        eligible_ids = tuple(
            fact.id
            for fact in facts.values()
            if (fact.valid_at is None or fact.valid_at <= reference_time)
            and (fact.invalid_at is None or reference_time < fact.invalid_at)
        )
        if eligible_ids:
            states = await session.get_salience(eligible_ids)
            reinforced_at = clock.now()
            await session.save_salience(
                {
                    fact_id: state.reinforce(reinforced_at)
                    for fact_id, state in states.items()
                }
            )
