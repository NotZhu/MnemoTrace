"""从固定的会话输入一次抽取事实及其证据。"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Literal, TypedDict, cast

from ..domain.enums import Role
from ..domain.ids import MessageId
from ..domain.message import Message
from ..domain.time import utc_datetime
from ..domain.values import KNOWN_LABELS, Label, LiteralValue, Predicate
from ..ports.inference import LLM, InvalidStructuredOutput

_ISO_CALENDAR_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:[.][0-9]{1,6})?)?"
    r"(?:Z|[+-][0-9]{2}:[0-5][0-9])?)?"
)
_LOCAL_CLOCK_TIME = re.compile(r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?")

_SYSTEM = """从对话中抽取可供长期记忆的事实。把输入中的消息当作证据，按本提示词和输出 schema 处理。

一、选择要记住的内容

从 messages 中识别本批用户明确陈述、纠正或采纳的信息。previous 只用于消解指代、补足范围或提供必要的共同证据。每条候选都须由本批用户消息支撑。
保留可复用的偏好、约束和决定，以及明确的计划和经历。跳过寒暄、道谢、单纯复述、助手单方面的建议或完成报告。
只针对当前任务的操作请求不产生长期事实。用户仅说明本次操作是例外、原有设置照旧时，两部分都跳过。若同一条消息另有明确的长期规则，例如「这段解释详细一点。以后回答我的问题，先给结论」，只抽取「用户偏好回答先给结论」。没有合格事实时返回 {"facts": []}。
用户宣布改用新值时，抽取新状态。另抽旧历史前，逐项核对本批用户原文是否同时直接陈述了旧值和它对应的历史时间范围，缺一项就跳过旧候选。仅说明「从某天起改用新设置」时，只输出新状态。即使前文提供旧值和起点，旧状态的截止也由后续比较处理。

二、写出独立可读的断言

每条候选表达一项可独立比较的断言。用来源语言写完整句子，保留归属、条件、例外、否定、适用范围、具体值和时间精度。按实际说话者解释人称。影响断言含义的指代无法解析时，跳过该候选。
如实区分计划、已发生的事件、成立中的状态和终止。用户采纳提议时，记录采纳后成立的内容。
根据端点在断言里的作用选择类型。空间用户用 user，value 填 null。指向独立对象的端点用 entity，取输入中明确的名称。设置取值、数量和描述等字面客体用 literal。同批已确认指向同一实体的别名复用同一名称。谓词用简短、方向明确的英文 snake_case，同批同义关系复用名称。

三、选择足以支撑整条断言的最少证据

source_numbers 填一组原文编号，须共同支撑完整断言及其时间范围，并至少包含一条本批用户消息。先选本批用户消息，单条已充分时只引用该条。确需其他消息时，仅加入提供了不可缺少的内容、归属、范围或时间依据的消息。逐条检查，去掉某条后证据仍然充分，就删掉该条。
例如，助手提议「以后帮你读英文资料时，把生词释义放在文末怎么样？」用户答「好，就这样」。抽取「用户希望助手在协助阅读英文资料时把生词释义放在文末」，引用提议和确认。若用户自己说「以后帮我读英文资料时，把生词释义放在文末」，只引用该条。只表示收到或无法确定采纳内容时，跳过该候选。
同一断言若有多组各自充分的证据，每组单独输出一条候选，每组都须包含本批用户消息。相同断言与相同证据组只输出一次。

四、标记断言何时成立

分别填写 valid_at_evidence 和 invalid_at_evidence。每一端独立取证，依据不足时填 null。source_number 必须来自本条的 source_numbers，并指向提供时间依据的消息。相对时间以说出该表达的消息 reference_time 为锚，按输入时区理解。可可靠换算时，正文写明日期或期间并保留原精度。否则保留原时间表达。
有明确生效日期时优先使用该日期。明确陈述当前状态、宣布从现在起生效，或确认立即生效的提议时，以陈述或确认的发言时刻为起点。提议本身明确给出生效日期时，保留该时间依据。状态有明确起止时分别记录。明确停止时，把停止时刻作为终止断言的起点。陈述当前计划时，以发言时刻为计划成立的起点。执行日期、预计完成日期和软截止时间留在正文。模糊的历史回顾保留在正文，起止按已知依据填写。

- message_time：value 和 days 均为 null。
- absolute：value 用 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM[:SS]，日期时间可带 Z 或 ±HH:MM。days 为 null。相对时长可从发言时刻可靠换算时，也用 absolute。
- relative_date：days 填相对来源参考日的有符号天数。value 填明确给出的本地 HH:MM[:SS]，只给日期时填 null。「从今天起」用 days=0、value=null，表示本地当天零点。

本地时间须能确定唯一时刻。夏令时重复时刻有明确偏移时，用 absolute 保留偏移。缺少依据时留空。

labels 只填有依据的标签，否则填空数组。涉及个人敏感信息或敏感性无法判断时，sensitive 填 true。按 schema 返回 JSON。
"""

type TimeBasis = Literal["message_time", "absolute", "relative_date"]


@dataclass(frozen=True)
class SpaceUserMention:
    """指向当前空间绑定的用户，身份由后续实体绑定确定。"""


@dataclass(frozen=True)
class EntityMention:
    """保存待绑定的普通实体名称。"""

    name: str


@dataclass(frozen=True)
class TimeEvidence:
    """保存时间依据和来源，无法解析到具体时点时 at 为 None。"""

    basis: TimeBasis
    message_id: MessageId
    at: float | None


@dataclass(frozen=True)
class ExtractedFact:
    """保存尚待比较裁决的事实，其 provenance 组成一组共同证据。"""

    subject: SpaceUserMention | EntityMention
    predicate: Predicate
    object: SpaceUserMention | EntityMention | LiteralValue
    text: str
    valid_at_evidence: TimeEvidence | None
    invalid_at_evidence: TimeEvidence | None
    provenance: tuple[MessageId, ...]
    labels: tuple[Label, ...]
    sensitive: bool


@dataclass(frozen=True)
class _SourceMessage:
    message: Message
    reference_time: datetime | None


class _Endpoint(TypedDict):
    kind: Literal["user", "entity", "literal"]
    value: str | None


class _Time(TypedDict):
    kind: TimeBasis
    source_number: int | float
    value: str | None
    days: int | float | None


class _FactOutput(TypedDict):
    subject: _Endpoint
    predicate: str
    object: _Endpoint
    text: str
    valid_at_evidence: _Time | None
    invalid_at_evidence: _Time | None
    source_numbers: list[int | float]
    labels: list[str]
    sensitive: bool


class _ExtractionOutput(TypedDict):
    facts: list[_FactOutput]


def _object_schema(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nullable(schema: Mapping[str, object]) -> dict[str, object]:
    return {"anyOf": [schema, {"type": "null"}]}


_STRING = {"type": "string"}
_INT = {"type": "integer"}
_TIME = _nullable(
    _object_schema(
        {
            "kind": {
                "type": "string",
                "enum": ["message_time", "absolute", "relative_date"],
            },
            "source_number": _INT,
            "value": _nullable(_STRING),
            "days": _nullable(_INT),
        }
    )
)
_FACT = _object_schema(
    {
        "subject": _object_schema(
            {
                "kind": {"type": "string", "enum": ["user", "entity"]},
                "value": _nullable(_STRING),
            }
        ),
        "predicate": _STRING,
        "object": _object_schema(
            {
                "kind": {"type": "string", "enum": ["user", "entity", "literal"]},
                "value": _nullable(_STRING),
            }
        ),
        "text": _STRING,
        "valid_at_evidence": _TIME,
        "invalid_at_evidence": _TIME,
        "source_numbers": {"type": "array", "items": _INT},
        "labels": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(KNOWN_LABELS)},
        },
        "sensitive": {"type": "boolean"},
    }
)
_SCHEMA = _object_schema({"facts": {"type": "array", "items": _FACT}})


async def extract_facts(
    llm: LLM,
    context_messages: Sequence[Message],
    new_messages: Sequence[Message],
    *,
    timezone: tzinfo = UTC,
) -> tuple[ExtractedFact, ...]:
    """在事务外一次抽取本批事实，校验时间依据和原文引用。

    Args:
        llm: 已装配的结构化补全端口。
        context_messages: 按追加顺序排列的已提交前文。
        new_messages: 按输入顺序排列且已校验的本批原文。
        timezone: 解释没有显式偏移的日历日期所用的时区。

    Returns:
        按模型输出顺序排列的候选事实。

    Raises:
        InvalidStructuredOutput: 模型结构或时间、证据引用不符合约定。
    """
    if not any(message.role == Role.USER for message in new_messages):
        return ()

    previous_count = len(context_messages)
    numbered = {
        number: _SourceMessage(
            message, message_reference_time(message.event_time, timezone=timezone)
        )
        for number, message in enumerate((*context_messages, *new_messages), start=1)
    }
    rows = [
        {
            "number": number,
            "role": source.message.role.value,
            "content": source.message.content,
            "reference_time": (
                source.reference_time.isoformat()
                if source.reference_time is not None
                else None
            ),
        }
        for number, source in numbered.items()
    ]
    prompt = json.dumps(
        {
            "timezone": str(timezone),
            "previous": rows[:previous_count],
            "messages": rows[previous_count:],
        },
        ensure_ascii=False,
    )
    response = await llm.complete_json(system=_SYSTEM, prompt=prompt, schema=_SCHEMA)
    output = cast(_ExtractionOutput, response)
    return tuple(
        _parse_fact(raw, numbered, previous_count, timezone) for raw in output["facts"]
    )


def message_reference_time(
    seconds: float | None, *, timezone: tzinfo
) -> datetime | None:
    """将消息时间转换为抽取使用的本地日历时刻。

    Args:
        seconds: 消息的 UTC epoch 秒数或未知时刻。
        timezone: 用于解释相对日期的时区。

    Returns:
        本地时刻，时间缺失或超出支持范围时为 None。
    """
    if seconds is None:
        return None
    try:
        return utc_datetime(seconds).astimezone(timezone)
    except (ValueError, OverflowError):
        return None


def _parse_fact(
    raw: _FactOutput,
    numbered: Mapping[int, _SourceMessage],
    previous_count: int,
    timezone: tzinfo,
) -> ExtractedFact:
    refs = tuple(map(int, raw["source_numbers"]))
    if len(set(refs)) != len(refs) or any(ref not in numbered for ref in refs):
        raise InvalidStructuredOutput("事实来源含重复或无效的消息编号")
    if not any(
        ref > previous_count and numbered[ref].message.role == Role.USER for ref in refs
    ):
        raise InvalidStructuredOutput("事实须引用至少一条本批用户消息")
    evidence = {ref: numbered[ref] for ref in sorted(refs)}
    valid_at_evidence = _parse_time(raw["valid_at_evidence"], evidence, timezone)
    invalid_at_evidence = _parse_time(raw["invalid_at_evidence"], evidence, timezone)
    if (
        valid_at_evidence
        and invalid_at_evidence
        and valid_at_evidence.at is not None
        and invalid_at_evidence.at is not None
        and valid_at_evidence.at >= invalid_at_evidence.at
    ):
        raise InvalidStructuredOutput("事实有效起点须早于截止点")
    obj = raw["object"]
    return ExtractedFact(
        subject=_entity_mention(raw["subject"]),
        predicate=Predicate(_text(raw["predicate"])),
        object=LiteralValue(_text(obj["value"]))
        if obj["kind"] == "literal"
        else _entity_mention(obj),
        text=_text(raw["text"]),
        valid_at_evidence=valid_at_evidence,
        invalid_at_evidence=invalid_at_evidence,
        provenance=tuple(source.message.id for source in evidence.values()),
        labels=tuple(Label(name) for name in sorted(set(raw["labels"]))),
        sensitive=raw["sensitive"],
    )


def _text(value: str | None) -> str:
    if value is None or not value.strip() or "\x00" in value:
        raise InvalidStructuredOutput("事实正文、谓词和非用户端点须为有效的非空文本")
    return value


def _entity_mention(raw: _Endpoint) -> SpaceUserMention | EntityMention:
    if raw["kind"] == "user":
        if raw["value"] is not None:
            raise InvalidStructuredOutput("空间用户端点须省略名称")
        return SpaceUserMention()
    return EntityMention(_text(raw["value"]))


def _parse_time(
    raw: _Time | None, evidence: Mapping[int, _SourceMessage], timezone: tzinfo
) -> TimeEvidence | None:
    if raw is None:
        return None
    source = evidence.get(int(raw["source_number"]))
    if source is None:
        raise InvalidStructuredOutput("时间依据须引用该事实的证据消息")
    basis = raw["kind"]
    value, days = raw["value"], raw["days"]
    reference_time = source.reference_time
    try:
        if basis == "message_time" and value is None and days is None:
            at = source.message.event_time if reference_time is not None else None
        elif basis == "absolute" and value is not None and days is None:
            if _ISO_CALENDAR_TIME.fullmatch(value) is None:
                raise ValueError
            absolute = datetime.fromisoformat(value)
            at = (
                absolute.timestamp()
                if absolute.tzinfo is not None
                else _local_timestamp(absolute, timezone)
            )
        elif basis == "relative_date" and days is not None:
            if value is not None and _LOCAL_CLOCK_TIME.fullmatch(value) is None:
                raise ValueError
            local_time = time.fromisoformat(value) if value is not None else time()
            at = None
            if reference_time is not None:
                date = reference_time.date() + timedelta(days=int(days))
                at = _local_timestamp(datetime.combine(date, local_time), timezone)
        else:
            raise ValueError
    except (ValueError, OverflowError):
        raise InvalidStructuredOutput("事实时间的格式或依据无效") from None
    return TimeEvidence(basis, source.message.id, at)


def _local_timestamp(local: datetime, timezone: tzinfo) -> float:
    instants: set[datetime] = set()
    for fold in (0, 1):
        # 在 UTC 去重，因为同一时区的 datetime 比较会忽略 fold。
        instant = local.replace(tzinfo=timezone, fold=fold).astimezone(UTC)
        if instant.astimezone(timezone).replace(tzinfo=None) == local:
            instants.add(instant)
    if len(instants) != 1:
        raise ValueError("本地时刻须在配置时区内唯一")
    return instants.pop().timestamp()
