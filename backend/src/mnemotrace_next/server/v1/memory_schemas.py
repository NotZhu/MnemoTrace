"""定义召回、事实和来源读取使用的 HTTP 请求与响应。"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from ...application.extraction import message_reference_time
from ...application.graph_recall import (
    DEFAULT_GRAPH_HOPS,
    MAX_EXPLICIT_ANCHORS,
    MAX_GRAPH_HOPS,
    MAX_GRAPH_PREDICATES,
    GraphRecallOptions,
)
from ...application.recall import (
    DEFAULT_TOP_K,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    RecallDegradedReason,
    RecallEvidence,
    RecallRelation,
    RecallResult,
)
from ...domain.enums import Role, Source
from ...domain.fact import Fact
from ...domain.ids import EntityId
from ...domain.message import Message
from ...domain.values import Predicate
from ...ports.queries import FactState
from .resource_ids import encode_resource_id
from .schemas import RFC3339, NonblankText, PageQuery, epoch_to_response_datetime

IdText = Annotated[NonblankText, StringConstraints(max_length=256)]
_QueryText = Annotated[NonblankText, StringConstraints(max_length=MAX_QUERY_CHARS)]


class GraphOptions(BaseModel):
    """限制一次图召回的起点、谓词与深度。"""

    model_config = ConfigDict(extra="forbid")

    origin_entity_ids: (
        Annotated[list[IdText], Field(max_length=MAX_EXPLICIT_ANCHORS)] | None
    ) = None
    predicates: (
        Annotated[
            list[NonblankText], Field(min_length=1, max_length=MAX_GRAPH_PREDICATES)
        ]
        | None
    ) = None
    max_hops: Annotated[int, Field(strict=True, ge=1, le=MAX_GRAPH_HOPS)] = (
        DEFAULT_GRAPH_HOPS
    )

    def to_core(self) -> GraphRecallOptions:
        """保留自动选取起点和显式空起点的区别。"""
        return GraphRecallOptions(
            origin_entity_ids=(
                tuple(EntityId(value) for value in self.origin_entity_ids)
                if self.origin_entity_ids is not None
                else None
            ),
            predicates=(
                tuple(Predicate(value) for value in self.predicates)
                if self.predicates is not None
                else None
            ),
            max_hops=self.max_hops,
        )


class RecallRequest(BaseModel):
    """查询单一空间内的事实证据。"""

    model_config = ConfigDict(extra="forbid")

    query: _QueryText
    top_k: Annotated[int, Field(strict=True, ge=1, le=MAX_TOP_K)] = DEFAULT_TOP_K
    as_of: RFC3339 | None = None
    graph: GraphOptions | None = None
    rerank: Annotated[bool, Field(strict=True)] | None = None
    prefer_recent: Annotated[bool, Field(strict=True)] = False
    track_usage: Annotated[bool, Field(strict=True)] = True
    include_sensitive: Annotated[bool, Field(strict=True)] = False


class FactListQuery(PageQuery):
    """筛选事实的业务有效状态并按键集分页。"""

    state: FactState = "current"
    as_of: RFC3339 | None = None
    include_sensitive: bool = False

    @model_validator(mode="after")
    def validate_state_time(self) -> Self:
        """完整时间线不使用有效时点过滤。"""
        if self.state == "all" and self.as_of is not None:
            raise ValueError("state 为 all 时须省略 as_of")
        return self


type FactTimeField = Literal["valid_at", "invalid_at", "recorded_at", "expired_at"]


class RelationResponse(BaseModel):
    """实体关系事实的两个端点和规范谓词。"""

    subject_entity_id: str
    predicate: str
    object_entity_id: str

    @classmethod
    def from_relation(cls, relation: RecallRelation) -> Self:
        """转换关系中的领域标识。"""
        return cls(
            subject_entity_id=relation.subject_id.value,
            predicate=relation.predicate.value,
            object_entity_id=relation.object_entity_id.value,
        )


class EvidenceResponse(BaseModel):
    """模型可使用的事实证据，不展开原文。"""

    fact_id: str
    text: str
    valid_at: datetime | None
    invalid_at: datetime | None
    out_of_range_fields: list[FactTimeField]
    source_message_ids: list[str]
    relation: RelationResponse | None


class _EvidenceFields(TypedDict):
    fact_id: str
    text: str
    valid_at: datetime | None
    invalid_at: datetime | None
    out_of_range_fields: list[FactTimeField]
    source_message_ids: list[str]
    relation: RelationResponse | None


def _evidence_fields(evidence: RecallEvidence) -> _EvidenceFields:
    out_of_range: list[FactTimeField] = []
    return {
        "fact_id": encode_resource_id(evidence.fact_id.value),
        "text": evidence.text,
        "valid_at": epoch_to_response_datetime(
            "valid_at", evidence.valid_at, out_of_range
        ),
        "invalid_at": epoch_to_response_datetime(
            "invalid_at", evidence.invalid_at, out_of_range
        ),
        "out_of_range_fields": out_of_range,
        "source_message_ids": [
            encode_resource_id(item.value) for item in evidence.source_message_ids
        ],
        "relation": (
            RelationResponse.from_relation(evidence.relation)
            if evidence.relation is not None
            else None
        ),
    }


class RecallResponse(BaseModel):
    """按相关性排序的证据、降级原因和实际重排状态。"""

    evidence: list[EvidenceResponse]
    degraded_reason: RecallDegradedReason | None
    rerank_applied: bool

    @classmethod
    def from_result(cls, result: RecallResult) -> Self:
        """按核心给出的顺序生成 HTTP 证据。"""
        return cls(
            evidence=[
                EvidenceResponse(**_evidence_fields(item)) for item in result.evidence
            ],
            degraded_reason=result.degraded_reason,
            rerank_applied=result.rerank_applied,
        )


class FactResponse(EvidenceResponse):
    """在证据基础上提供事实的收录、首次获知截止和可见性信息。"""

    recorded_at: datetime | None
    expired_at: datetime | None
    sensitive: bool
    source: Source
    labels: list[str]

    @classmethod
    def from_fact(cls, fact: Fact) -> Self:
        """将存储事实转换为公开详情。"""
        evidence = _evidence_fields(RecallEvidence.from_fact(fact))
        out_of_range = evidence["out_of_range_fields"]
        recorded_at = epoch_to_response_datetime(
            "recorded_at", fact.recorded_at, out_of_range
        )
        expired_at = epoch_to_response_datetime(
            "expired_at", fact.expired_at, out_of_range
        )
        return cls(
            **evidence,
            recorded_at=recorded_at,
            expired_at=expired_at,
            sensitive=fact.sensitive,
            source=fact.source,
            labels=[label.value for label in fact.labels],
        )


class FactPage(BaseModel):
    """可见事实的一页结果。"""

    items: list[FactResponse]
    next_cursor: str | None
    evaluated_at: datetime


class MessageResponse(BaseModel):
    """独立读取的一条会话原文。"""

    message_id: str
    session_id: str
    role: Role
    content: str
    event_time: datetime | None
    recorded_at: datetime | None
    out_of_range_fields: list[Literal["recorded_at"]]

    @classmethod
    def from_message(cls, message: Message) -> Self:
        """以 UTC 表示原文的事件和收录时间。"""
        out_of_range: list[Literal["recorded_at"]] = []
        return cls(
            message_id=encode_resource_id(message.id.value),
            session_id=encode_resource_id(message.session_id.value),
            role=message.role,
            content=message.content,
            event_time=message_reference_time(message.event_time, timezone=UTC),
            recorded_at=epoch_to_response_datetime(
                "recorded_at", message.recorded_at, out_of_range
            ),
            out_of_range_fields=out_of_range,
        )


class MessagePage(BaseModel):
    """按追加位置分页的会话原文。"""

    items: list[MessageResponse]
    next_cursor: str | None
