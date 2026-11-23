"""评测阶段共享的数据类型，隔离查询输入和评分标注。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type Benchmark = Literal["longmemeval-s", "locomo", "personamem-v2"]
type BackendName = Literal["mnemotrace", "full-context", "bm25"]
type Stage = Literal["ingest", "retrieve", "answer", "score", "cleanup"]
type Role = Literal["user", "assistant", "system"]


@dataclass(frozen=True, slots=True)
class Turn:
    """保留上游发言标识、顺序、说话人和可用时间。"""

    id: str
    role: Role
    text: str
    event_time: str | None = None
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class Session:
    """一段按输入顺序排列的原始会话。"""

    id: str
    turns: tuple[Turn, ...]
    event_time: str | None = None


@dataclass(frozen=True, slots=True)
class Query:
    """提供给检索或回答阶段的公开问题。"""

    text: str
    event_time: str | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Reference:
    """仅由评分阶段读取的答案和证据标识。"""

    category: str
    answer: str
    evidence: tuple[str, ...] = ()
    abstention: bool = False
    correct_choice: str | None = None
    unmapped_evidence: tuple[str, ...] = ()

    @property
    def has_retrieval_labels(self) -> bool:
        """判断题目是否有完整可核验的检索标注。"""
        return bool(self.evidence) and not (self.abstention or self.unmapped_evidence)


@dataclass(frozen=True, slots=True)
class Case:
    """将题目与其历史及评分标注关联。"""

    id: str
    history_id: str
    query: Query
    reference: Reference


class Record(BaseModel):
    """规范持久化记录的解析行为。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Record):
    """记录一条召回文本和可核验的来源。"""

    text: str
    session_ids: tuple[str, ...] = ()
    turn_ids: tuple[str, ...] = ()


class Retrieval(Record):
    """同时保留候选证据和原始响应。"""

    evidence: tuple[Evidence, ...]
    provenance: bool
    raw: JsonValue = None


class ModelCall(Record):
    """保留一次请求的响应和已测得的 token 计数。"""

    status: int | None = Field(ge=100, le=599)
    raw: JsonValue
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class Completion(Record):
    """记录模型响应和供应商返回的实际用量。"""

    text: str
    model: str
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    raw: JsonValue = None


class Answer(Record):
    """保留实际送给回答模型的上下文和提示词。"""

    context: str
    prompt: str
    context_tokens: int = Field(ge=0)
    completion: Completion


class ContextEvidence(Record):
    """记录回答上下文完整或部分保留的金标会话来源。"""

    gold_session_count: int = Field(ge=1)
    covered_session_ids: tuple[str, ...]
    partial_session_ids: tuple[str, ...]


class Score(Record):
    """记录可重算的逐题评分及其原始裁判输出。"""

    correct: bool
    category: str
    abstention: bool
    retrieval: dict[str, float] | None = None
    context_evidence: ContextEvidence | None = None
    judge_prompt: str | None = None
    judge: Completion | None = None
