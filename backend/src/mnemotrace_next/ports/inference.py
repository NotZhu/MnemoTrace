"""定义文本生成、结构化补全和批量嵌入端口。"""

from collections.abc import Mapping, Sequence
from typing import NamedTuple, Protocol


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
    """模型输出无法解析为符合请求 schema 的对象。"""


class InferenceError(RuntimeError):
    """推理请求失败或上游响应协议异常。"""


class LLM(Protocol):
    """提供单次文本或结构化补全。"""

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

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
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

        Returns:
            已通过 schema 校验的 JSON 对象。

        Raises:
            ValueError: 请求 schema 无效或包含不支持的结构。
            CompletionTruncated: 输出在完成之前被截断。
            CompletionRefused: 模型拒绝执行请求。
            InvalidStructuredOutput: 输出无法解析或不符合 schema。
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

        适配器校验模型输出的批次大小、向量长度和分量有限性。

        Args:
            texts: 待嵌入的文本序列。

        Returns:
            向量序列，每条向量长度为 dim。空输入对应空序列。

        Raises:
            InferenceError: 请求失败或上游返回无效向量。
            TimeoutError: 请求或整批嵌入超过时限。
        """
        ...
