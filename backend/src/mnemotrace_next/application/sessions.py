"""管理记忆写入所需的对话身份。"""

from ..domain.ids import SessionId, SpaceId
from ..ports.runtime import IdGenerator
from ..ports.storage import Storage


async def create_session(
    storage: Storage, space_id: SpaceId, ids: IdGenerator
) -> SessionId:
    """为已授权空间创建可用于写入的对话。

    Args:
        storage: 已绑定认证账号的存储端口。
        space_id: 对话所属空间。
        ids: 由服务端装配的标识生成器。

    Returns:
        成功提交的对话标识。

    Raises:
        LookupError: 空间不可访问。
        ValueError: 生成的标识与有效登记冲突。
    """
    async with storage.session(space_id, write=True) as writer:
        session_id = ids.new_session_id()
        await writer.register_session(session_id)
    return session_id
