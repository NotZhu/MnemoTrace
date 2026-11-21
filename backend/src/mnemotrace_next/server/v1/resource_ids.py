"""在 HTTP 路径和存量资源标识之间转换。"""

import re
from base64 import b64decode, urlsafe_b64encode
from binascii import Error

_PLAIN = re.compile(r"[A-Za-z0-9._~-]+")
_ENCODED = re.compile(r"[A-Za-z0-9_-]+")
_PREFIX = "~"


def encode_resource_id(value: str) -> str:
    """生成可直接放入一个 URL 路径段的公开标识。"""
    if (
        not value.startswith(_PREFIX)
        and value not in (".", "..")
        and _PLAIN.fullmatch(value)
    ):
        return value
    return _PREFIX + urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip(
        "="
    )


def decode_resource_id(value: str) -> str:
    """将带保留前缀的标识还原为存储原值。

    Args:
        value: 已由 HTTP 框架解析的单个路径段。

    Returns:
        存储使用的资源标识。

    Raises:
        ValueError: 保留前缀后的编码无效或不是规范形式。
    """
    if not value.startswith(_PREFIX):
        return value
    encoded = value[len(_PREFIX) :]
    if not _ENCODED.fullmatch(encoded):
        raise ValueError("资源标识编码无效")
    try:
        decoded = b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        ).decode("utf-8")
    except (Error, UnicodeDecodeError):
        raise ValueError("资源标识编码无效") from None
    if encode_resource_id(decoded) != value:
        raise ValueError("资源标识编码无效")
    return decoded
