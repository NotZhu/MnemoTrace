"""定义文本生成、结构化补全和批量嵌入端口。"""

from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from numbers import Real
from typing import NamedTuple, Protocol, overload


class Completion(NamedTuple):
    """保存模型补全正文及结束原因。

    Attributes:
        text: 生成的正文。
        finish_reason: 供应商报告的结束原因，缺失时为 None。
    """

    text: str
    finish_reason: str | None = None


class CompletionTruncated(RuntimeError):
    """模型输出在完成之前被截断。"""


class CompletionRefused(RuntimeError):
    """模型拒绝执行本次请求。"""


class InvalidStructuredOutput(RuntimeError):
    """模型输出违反 JSON、schema 或应用契约。

    Args:
        rule: 违反的规则，不含原始输入或模型输出。
        path: 可选的字段路径，只使用 schema 字段名和数组索引。
    """

    def __init__(self, rule: str, *, path: str | None = None) -> None:
        self.rule = rule
        self.path = path
        super().__init__(f"字段路径 {path}：{rule}" if path else rule)


class InferenceError(RuntimeError):
    """推理请求失败或上游响应协议异常。"""


class InferenceInputTooLarge(InferenceError):
    """完整补全输入超出模型的上下文预算。"""


class LLM(Protocol):
    """提供单次文本或结构化补全。"""

    def structured_prompt_fits(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> bool:
        """检查结构化请求及一次校验反馈能否装入该模型的输入预算。"""
        ...

    async def complete(self, *, system: str, prompt: str) -> Completion:
        """根据系统指令和提示生成完整文本。

        适配器将供应商的截断和拒答状态转换为对应异常。

        Args:
            system: 系统指令。
            prompt: 待补全的提示。

        Returns:
            可编码为 UTF-8 的完整文本及供诊断的结束原因。

        Raises:
            CompletionTruncated: 输出在完成之前被截断。
            CompletionRefused: 模型拒绝执行请求。
            InferenceError: 请求失败或上游响应协议异常。
            TimeoutError: 请求或整次补全超过时限。
        """
        ...

    @overload
    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: None = None,
        enforce_context_budget: bool = False,
    ) -> Mapping[str, object]: ...

    @overload
    async def complete_json[T](
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: Callable[[Mapping[str, object]], T],
        enforce_context_budget: bool = False,
    ) -> T: ...

    async def complete_json[T](
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: Callable[[Mapping[str, object]], T] | None = None,
        enforce_context_budget: bool = False,
    ) -> Mapping[str, object] | T:
        """生成符合请求 schema 的 JSON 对象。

        根对象及嵌套对象用 properties 声明属性，全部成员列入 required，
        并设置 additionalProperties 为 false。数组用单一 items schema。
        标量支持 string、integer、number、boolean、null 及标量 enum。
        可空字段用两项 anyOf 表达，一项为 null，另一项为受支持的非空 schema。
        适配器在返回前验证模型输出的结构、字段类型和字符串的 UTF-8 合法性。

        Args:
            system: 系统指令。
            prompt: 待补全的提示。
            schema: 描述结构化输出的受支持 JSON Schema 子集。
            parse: 在 schema 校验后解析应用对象。其 InvalidStructuredOutput
                参与同一次补全的纠错预算。
            enforce_context_budget: 为真时在每次发送前检查完整结构化输入。

        Returns:
            直接返回已通过 schema 校验的对象，或 parse 转换后的结果。

        Raises:
            ValueError: 请求 schema 无效或包含不支持的结构。
            CompletionTruncated: 输出在完成之前被截断。
            CompletionRefused: 模型拒绝执行请求。
            InvalidStructuredOutput: 输出无法解析或违反 schema 或应用契约。
            InferenceError: 请求失败或上游响应协议异常。
            TimeoutError: 请求或整次补全超过时限。
        """
        ...


class Embedder(Protocol):
    """将输入文本转换为同一嵌入空间中的向量。"""

    @property
    def embedding_space(self) -> str:
        """标识生成向量所用的嵌入空间。

        Returns:
            可供索引写入和查询共用的稳定模型及版本标识。
        """
        ...

    @property
    def dim(self) -> int:
        """读取此端口生成的向量维度。

        Returns:
            每条向量的固定分量数，为正整数。
        """
        ...

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """按输入顺序生成一一对应的向量。

        适配器校验模型输出的批次大小、向量长度、分量有限性与非零要求。

        Args:
            texts: 待嵌入的文本序列。

        Returns:
            向量序列，每条向量长度为 dim，至少一个分量非零，分量须为有限实数
            且不含布尔值。空输入对应空序列。

        Raises:
            InferenceError: 请求失败或上游返回无效向量。
            TimeoutError: 请求或整批嵌入超过时限。
        """
        ...


def validate_embedding_batch(
    vectors: object, *, expected_count: int, dimension: int
) -> tuple[tuple[float, ...], ...]:
    """校验嵌入结果并按原顺序返回浮点向量。

    Args:
        vectors: 推理返回的向量批次。
        expected_count: 本批输入文本数。
        dimension: 嵌入端口声明的维度。

    Returns:
        通过校验的浮点向量元组。

    Raises:
        InferenceError: 批次数量、向量维度或向量分量违反端口契约。
    """
    if (
        not isinstance(vectors, Sequence)
        or isinstance(vectors, (str, bytes, bytearray))
        or len(vectors) != expected_count
    ):
        raise InferenceError("嵌入结果的向量数量须与输入数量一致")

    batch: list[tuple[float, ...]] = []
    for vector in vectors:
        if not isinstance(vector, Sequence) or isinstance(
            vector, (str, bytes, bytearray)
        ):
            raise InferenceError("嵌入结果须返回数值序列")
        if not vector or len(vector) != dimension:
            raise InferenceError("嵌入向量维度与 Embedder 配置不一致")
        components: list[float] = []
        for value in vector:
            # 原生数值占多数，优先跳过 Real 的抽象类型检查。
            if type(value) not in (float, int) and (
                isinstance(value, bool) or not isinstance(value, Real)
            ):
                raise InferenceError("嵌入向量分量须为有限实数")
            try:
                component = float(value)
            except (OverflowError, TypeError, ValueError):
                raise InferenceError("嵌入向量分量须为有限实数") from None
            if not isfinite(component):
                raise InferenceError("嵌入向量分量须为有限实数")
            components.append(component)
        if not any(components):
            raise InferenceError("嵌入向量不能全为零")
        batch.append(tuple(components))
    return tuple(batch)
