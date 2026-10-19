"""记忆 space + 访问控制（CLAUDE.md §6——多 agent / 团队共享记忆）。

*space* 就是记忆命名空间——正是 `MemoryService` 已有的键。一个 API key（一个 *principal*）
可以写一个 space、读多个：自己的 + 共享团队 space + 某个终端用户的。
这等价于 Mem0/Letta/Zep 暴露的 user/agent/session 作用域，叠加在现有的按命名空间存储之上，
完全不改动记忆引擎：服务本就按 space 键控，写只是指向一个（校验过的）命名空间字符串，
读则是对多个 space 扇出再融合。

更丰富的 principal 用 MNEMOTRACE_SPACES（JSON）配置：

    MNEMOTRACE_SPACES='{"sk-team":{"home":"agentA","read":["agentA","team"],"write":["agentA","team"]}}'

省略 `read`/`write` 时默认为 [home]；home space 对其 owner 始终可读可写。
单 space 认证（MNEMOTRACE_API_KEYS / MNEMOTRACE_OPEN）保持不变，由服务端的 `auth` 处理——
本模块解析 MNEMOTRACE_SPACES 的 principal，并为两类配置共享凭证字符规则和配置异常。
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Optional


class AuthConfigError(ValueError):
    """静态鉴权配置无效；错误信息不得包含凭证。"""


def is_valid_api_key(key: str) -> bool:
    """与控制台一致：非空、可打印 ASCII；不编码或改写凭证身份。"""
    return bool(key.strip()) and all(" " <= char <= "~" for char in key)


@dataclass
class Principal:
    """调用者是谁、可以触碰哪些 space。`home` 是默认读写的命名空间。"""

    home: str
    readable: list[str] = field(default_factory=list)
    writable: list[str] = field(default_factory=list)

    def can_read(self, space: str) -> bool:
        return space in self.readable

    def can_write(self, space: str) -> bool:
        return space in self.writable

    @classmethod
    def single(cls, user: str) -> "Principal":
        """向后兼容的一键一命名空间 principal（MNEMOTRACE_API_KEYS / 开放模式）。"""
        return cls(home=user, readable=[user], writable=[user])


def load_spaces() -> dict[str, dict]:
    """MNEMOTRACE_SPACES (JSON) -> {api_key: {home, read[], write[]}}。格式错误的 JSON 视同未设置——
    坏值绝不能悄悄授予或扩大访问权限。"""
    raw = os.environ.get("MNEMOTRACE_SPACES", "").strip()
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(cfg, dict):
        return {}
    # 先校验整份配置，避免早先匹配成功掩盖后续不可比较的 key。
    for index, key in enumerate(cfg, start=1):
        if not is_valid_api_key(key):
            raise AuthConfigError(f"MNEMOTRACE_SPACES entry {index} key must be non-empty printable ASCII")
    return cfg


def _principal_from_spec(spec: dict) -> Optional[Principal]:
    if not isinstance(spec, dict):
        return None
    home = str(spec.get("home") or "").strip()
    if not home:
        return None  # space principal 必须声明 home 命名空间
    read = [str(s) for s in (spec.get("read") or [home])]
    write = [str(s) for s in (spec.get("write") or [home])]
    # 无论 read/write 如何列举，owner 对自己的 home 始终可读可写
    if home not in read:
        read = [home, *read]
    if home not in write:
        write = [home, *write]
    return Principal(home=home, readable=read, writable=write)


def principal_for_token(token: str) -> Optional[Principal]:
    """对照 MNEMOTRACE_SPACES 解析 bearer token，不匹配任何已配置 space key 时返回 None。
    与服务端 key 认证一样使用常数时间比较（同样的加固，同样的理由）。"""
    if not token or not is_valid_api_key(token):
        return None
    token = token.strip()
    for key, spec in load_spaces().items():
        if hmac.compare_digest(token, str(key)):
            return _principal_from_spec(spec)
    return None
