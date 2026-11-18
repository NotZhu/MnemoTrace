"""检查端口规定的 schema 子集并校验模型正文。"""

import json
from collections.abc import Mapping
from math import isfinite
from typing import NoReturn, cast

from jsonschema import Draft202012Validator, ValidationError

from ...ports.inference import InvalidStructuredOutput

_SCALARS = ("string", "integer", "number", "boolean", "null")


def prepare_validator(schema: Mapping[str, object]) -> Draft202012Validator:
    """规范化并检查应用提供的受支持 schema。

    Args:
        schema: 描述输出对象的端口 schema。

    Returns:
        可验证实例且持有规范化 schema 的校验器。

    Raises:
        ValueError: schema 形状无效或使用了子集之外的功能。
    """
    if schema.get("type") != "object":
        raise ValueError("结构化输出 schema 的根节点须为对象")
    try:
        return Draft202012Validator(_schema_node(schema))
    except RecursionError:
        raise ValueError("结构化输出 schema 的嵌套层数过多") from None


def decode_output(text: str, validator: Draft202012Validator) -> dict[str, object]:
    """解析并校验完整的模型正文。

    Args:
        text: 模型输出的原始正文。
        validator: 已按端口子集检查的校验器。

    Returns:
        符合 schema 的 JSON 对象。

    Raises:
        InvalidStructuredOutput: 正文不可解析或违反 schema。
    """
    try:
        value: object = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
        _check_unicode(value)
    except UnicodeEncodeError:
        raise InvalidStructuredOutput(
            "JSON 字符串含未配对的 Unicode 代理字符"
        ) from None
    except (ValueError, RecursionError):
        raise InvalidStructuredOutput("请在根路径返回单个合法 JSON 对象") from None
    if not isinstance(value, dict):
        raise InvalidStructuredOutput("根路径违反 object 规则")
    try:
        validator.validate(value)
    except ValidationError as exc:
        path = json.dumps(list(exc.absolute_path), ensure_ascii=False)
        raise InvalidStructuredOutput(
            f"字段路径 {path} 违反 {exc.validator} 规则"
        ) from None
    return cast(dict[str, object], value)


def _check_unicode(value: object) -> None:
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            _check_unicode(child)
    elif isinstance(value, list):
        for item in value:
            _check_unicode(item)


def _schema_node(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        # 端口将所有不受支持的 schema 统一报告为 ValueError。
        raise ValueError("每个 schema 节点须为对象")  # noqa: TRY004
    node = cast(Mapping[str, object], value)
    kind = node.get("type")
    if set(node) == {"anyOf"}:
        branches = node["anyOf"]
        if not isinstance(branches, (list, tuple)) or len(branches) != 2:
            raise ValueError("可空 anyOf 须恰好有两个分支")
        options = [_schema_node(branch) for branch in branches]
        first, second = options
        null, non_null = (
            (first, second) if first.get("type") == "null" else (second, first)
        )
        if null.get("type") != "null" or Draft202012Validator(non_null).is_valid(None):
            raise ValueError("可空 anyOf 须有一个 null 分支和一个非空分支")
        return {"anyOf": options}
    if kind == "object":
        if set(node) != {"type", "properties", "required", "additionalProperties"}:
            raise ValueError(
                "对象 schema 须声明 properties、required 和 additionalProperties"
            )
        props, required = node["properties"], node["required"]
        if (
            node["additionalProperties"] is not False
            or not isinstance(props, Mapping)
            or not all(isinstance(key, str) for key in props)
            or not isinstance(required, (list, tuple))
            or not all(isinstance(key, str) for key in required)
            or len(required) != len(props)
            or set(required) != set(props)
        ):
            raise ValueError("对象 schema 须要求全部属性且关闭额外属性")
        return {
            "type": "object",
            "properties": {key: _schema_node(child) for key, child in props.items()},
            "required": list(required),
            "additionalProperties": False,
        }
    if kind == "array" and set(node) == {"type", "items"}:
        return {"type": "array", "items": _schema_node(node["items"])}
    if kind in _SCALARS and set(node) == {"type"}:
        return dict(node)
    if set(node) == {"enum"} or (kind in _SCALARS and set(node) == {"type", "enum"}):
        members = node["enum"]
        if (
            not isinstance(members, (list, tuple))
            or not members
            or not all(_json_scalar(member) for member in members)
        ):
            raise ValueError("enum 须包含至少一个 JSON 标量")
        if "type" in node:
            scalar = Draft202012Validator({"type": kind})
            if not all(scalar.is_valid(member) for member in members):
                raise ValueError("enum 成员须符合声明的标量类型")
        return {**node, "enum": list(members)}
    raise ValueError("schema 使用了端口子集之外的结构或关键字")


def _json_scalar(value: object) -> bool:
    return (
        value is None
        or isinstance(value, (str, bool, int))
        or isinstance(value, float)
        and isfinite(value)
    )


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("JSON 不允许非有限数")


def _finite_float(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("JSON 数值超出有限浮点范围")
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON 对象包含重复键")
    return result
