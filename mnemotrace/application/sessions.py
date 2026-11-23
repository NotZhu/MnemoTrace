"""管理记忆写入所需的对话身份。"""

from ..domain.ids import SessionId, SpaceId
from ..ports.runtime import IdGenerator
from ..ports.storage import SessionRegistration, Storage


async def create_session(
    storage: Storage, space_id: SpaceId, ids: IdGenerator
) -> SessionId:
    """为已授权空间创建对话并返回标识。"""
    return (await create_session_registration(storage, space_id, ids)).session_id


async def create_session_registration(
    storage: Storage, space_id: SpaceId, ids: IdGenerator
) -> SessionRegistration:
    """为已授权空间创建并返回完整的对话登记。

    Args:
        storage: 已绑定认证账号的存储端口。
        space_id: 对话所属空间。
        ids: 由服务端装配的标识生成器。

    Returns:
        成功提交的对话登记。

    Raises:
        LookupError: 空间不可访问。
        ValueError: 生成的标识与有效登记冲突。
    """
    async with storage.session(space_id, write=True) as writer:
        registration = await writer.register_session(ids.new_session_id())
    return registration
