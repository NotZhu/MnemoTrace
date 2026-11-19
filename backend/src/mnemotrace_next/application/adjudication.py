"""准备待比较的事实，并在事务外批量裁决。"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from math import fsum, hypot
from typing import Literal, TypedDict, cast

from ..domain.entity import Entity
from ..domain.enums import Source
from ..domain.fact import Fact, intervals_disjoint
from ..domain.ids import EntityId, FactId, SpaceId
from ..domain.values import EntityRef, LiteralValue
from ..ports.inference import LLM, Embedder, InvalidStructuredOutput
from ..ports.queries import FactCursor, FactFilter, FactQuery, FactSlot
from ..ports.runtime import IdGenerator
from ..ports.storage import SpaceReader
from .extraction import EntityMention, ExtractedFact, SpaceUserMention, TimeEvidence

_SEARCH_LIMIT = 10
_MIN_SIMILARITY = 0.60

type FactPair = tuple[FactId, FactId]
type Verdict = Literal["duplicate", "coexists", "conflict", "unknown"]

_SYSTEM = """比较 pairs 指定的事实，每对返回恰好一个判定，原样返回配对 id。facts 是待分析的数据。

subject 和 object.entity 共用一套实体编号。同号表示同一个已绑定实体。编号不同时按各自身份理解，同名本身不足以确认是同一实体。
根据关系语义核对参与者的角色，允许正反句式表达同一关系。区分已发生的事件、计划和有效状态，保留陈述的归属。

duplicate 表示同一断言，参与者、角色、条件、范围、肯定或否定、关键值和事件实例一致。不同时间确认同一个持续状态，可以判为 duplicate。
coexists 表示两条事实可以同时成立或明确无关。不同属性、可同时成立的多个值、另一次发生的事件都属于这一类。
conflict 表示同一关系或状态在同一适用范围内明确互斥，例如明确终止、交接，或同一个默认设置出现互斥的取值。
unknown 用于可能在说同一项事实，但实体身份、角色或适用范围不足以确定关系的情况。

「用户住在杭州」与「用户的居住地是杭州」判 duplicate。
「用户喜欢喝茶」与「用户喜欢咖啡」判 coexists。
「用户住在杭州」与「用户住在上海」判 conflict。
前后两次确认默认用中文，判 duplicate。两天分别去过同一家店，判 coexists。

历史时段陈述只说明该时段内的状态。有效期先后、证据覆盖和实际截止由后端计算。相似词句、不同日期或一条事实更新近均不足以判矛盾。
"""

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["duplicate", "coexists", "conflict", "unknown"],
                    },
                },
                "required": ["id", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class PreparedFact:
    """保存已绑定的候选及可供后续写入复用的输入。"""

    fact: Fact
    entities: tuple[Entity, ...]
    vector: tuple[float, ...]
    valid_at_evidence: TimeEvidence | None
    invalid_at_evidence: TimeEvidence | None


@dataclass(frozen=True)
class ComparisonCandidates:
    """保存同一授权快照内命中的旧事实及其稳定配对顺序。"""

    existing_facts: Mapping[FactId, Fact]
    pairs: tuple[FactPair, ...]


@dataclass(frozen=True)
class FactComparison:
    """保存单对事实的裁决及其是否仅由区间错开推得。"""

    pair: FactPair
    verdict: Verdict
    disjoint: bool = False


class _Decision(TypedDict):
    id: str
    verdict: Verdict


class _ComparisonOutput(TypedDict):
    decisions: list[_Decision]


async def prepare_facts(
    extracted: Sequence[ExtractedFact],
    *,
    space_id: SpaceId,
    embedder: Embedder,
    ids: IdGenerator,
    received_at: float,
) -> tuple[PreparedFact, ...]:
    """绑定实体并为整批候选生成可复用的正文向量。

    Args:
        extracted: 已校验的抽取结果，按模型输出顺序排列。
        space_id: 已授权且绑定的空间。
        embedder: 写入索引使用的嵌入端口。
        ids: 为每条候选分配固定标识。
        received_at: 本次请求的接收时刻。

    Returns:
        按抽取顺序排列的已绑定候选。
    """
    if not extracted:
        return ()
    bound: list[tuple[Fact, tuple[Entity, ...]]] = []
    for item in extracted:
        subject = _bind_entity(item.subject, space_id)
        entities: tuple[Entity, ...] = (subject,)
        if isinstance(item.object, LiteralValue):
            object_value: EntityRef | LiteralValue = item.object
        else:
            target = _bind_entity(item.object, space_id)
            entities = (subject, target)
            object_value = EntityRef(target.id)

        valid_at = item.valid_at_evidence.at if item.valid_at_evidence else None
        invalid_at = item.invalid_at_evidence.at if item.invalid_at_evidence else None
        fact = Fact(
            id=ids.new_fact_id(),
            space_id=space_id,
            subject=EntityRef(subject.id),
            predicate=item.predicate,
            object=object_value,
            text=item.text,
            valid_at=valid_at,
            invalid_at=invalid_at,
            recorded_at=received_at,
            expired_at=received_at if invalid_at is not None else None,
            provenance=item.provenance,
            labels=item.labels,
            sensitive=item.sensitive,
            source=Source.EXTRACTED,
        )
        bound.append((fact, entities))
    vectors = await embedder.embed(tuple(fact.text for fact, _ in bound))
    return tuple(
        PreparedFact(
            fact,
            entities,
            tuple(vector),
            item.valid_at_evidence,
            item.invalid_at_evidence,
        )
        for (fact, entities), item, vector in zip(
            bound, extracted, vectors, strict=True
        )
    )


def _bind_entity(mention: EntityMention | SpaceUserMention, space: SpaceId) -> Entity:
    if isinstance(mention, SpaceUserMention):
        return Entity.space_user(space)
    return Entity(space, mention.name)


def _slot(fact: Fact) -> FactSlot:
    return fact.subject, fact.predicate


async def find_comparison_candidates(
    reader: SpaceReader,
    prepared: Sequence[PreparedFact],
    *,
    embedding_space: str,
    reference_time: float,
) -> ComparisonCandidates:
    """用同一快照读取旧事实并生成新旧和批内比较对。

    Args:
        reader: 绑定已授权空间的读快照或提交复核会话。
        prepared: 本批已绑定和嵌入的候选。
        embedding_space: 与候选向量和索引匹配的模型标识。
        reference_time: 本次写入共用的请求时刻。

    Returns:
        命中的旧事实和按裁决顺序排列的去重配对。
    """
    existing: dict[FactId, Fact] = {}
    by_slot: dict[FactSlot, tuple[FactId, ...]] = {}
    old_pairs: list[list[FactId]] = []
    for item in prepared:
        fact = item.fact
        slot = _slot(fact)
        if slot not in by_slot:
            timeline = await _read_timeline(reader, slot, reference_time)
            existing.update((old.id, old) for old in timeline)
            by_slot[slot] = tuple(old.id for old in timeline)
        filters = FactFilter(
            state="all",
            overlap_start=fact.valid_at,
            overlap_end=fact.invalid_at,
            exclude_slot=slot,
        )
        lexical = await reader.search_lexical(
            fact.text,
            _SEARCH_LIMIT,
            filters=filters,
            reference_time=reference_time,
        )
        vectors = await reader.search_vectors(
            item.vector,
            _SEARCH_LIMIT,
            filters=filters,
            reference_time=reference_time,
            embedding_space=embedding_space,
            min_score=_MIN_SIMILARITY,
        )
        old_ids = set(by_slot[slot])
        old_ids.update(fact_id for fact_id, _ in lexical)
        old_ids.update(fact_id for fact_id, _ in vectors)
        old_pairs.append(sorted(old_ids, key=lambda fact_id: fact_id.value))

    missing = dict.fromkeys(
        fact_id for ids in old_pairs for fact_id in ids if fact_id not in existing
    )
    if missing:
        existing.update(await reader.get_facts(tuple(missing)))
    batch_pairs = _batch_pairs(prepared)
    pairs: list[FactPair] = []
    for index, item in enumerate(prepared):
        pairs.extend((item.fact.id, old_id) for old_id in old_pairs[index])
        pairs.extend(
            (item.fact.id, prepared[peer].fact.id) for peer in batch_pairs[index]
        )
    return ComparisonCandidates(existing, tuple(pairs))


async def _read_timeline(
    reader: SpaceReader, slot: FactSlot, reference_time: float
) -> list[Fact]:
    subject, predicate = slot
    filters = FactFilter(state="all", subject=subject, predicate=predicate)
    query = FactQuery(filters=filters)
    facts: list[Fact] = []
    while True:
        page = await reader.query_facts(query, reference_time=reference_time)
        facts.extend(page)
        if len(page) < query.limit:
            return facts
        last = page[-1]
        query = FactQuery(filters, FactCursor(last.recorded_at, last.id), query.limit)


def _batch_pairs(prepared: Sequence[PreparedFact]) -> list[list[int]]:
    pairs: list[set[int]] = [set() for _ in prepared]
    if len(prepared) < 2:
        return [list(peers) for peers in pairs]
    neighbors: list[list[tuple[float, int]]] = [[] for _ in prepared]
    vectors = [_unit_vector(item.vector) for item in prepared]
    slots = [_slot(item.fact) for item in prepared]
    for left, right in combinations(range(len(prepared)), 2):
        if slots[left] == slots[right]:
            pairs[left].add(right)
            continue
        if intervals_disjoint(prepared[left].fact, prepared[right].fact):
            continue
        score = fsum(x * y for x, y in zip(vectors[left], vectors[right], strict=True))
        if score >= _MIN_SIMILARITY:
            neighbors[left].append((-score, right))
            neighbors[right].append((-score, left))
    for index, peers in enumerate(neighbors):
        for _, peer in sorted(peers)[:_SEARCH_LIMIT]:
            left, right = sorted((index, peer))
            pairs[left].add(right)
    return [sorted(peers) for peers in pairs]


def _unit_vector(vector: Sequence[float]) -> tuple[float, ...]:
    # 先等比缩放，使大分量的有限向量也能计算有限的范数。
    scale = max(map(abs, vector))
    scaled = tuple(component / scale for component in vector)
    norm = hypot(*scaled)
    return tuple(component / norm for component in scaled)


async def compare_facts(
    llm: LLM,
    prepared: Sequence[PreparedFact],
    candidates: ComparisonCandidates,
) -> tuple[FactComparison, ...]:
    """用一次结构化调用裁决所有无法直接判断的配对。

    Args:
        llm: 已装配的结构化模型端口。
        prepared: 本批已绑定的事实候选。
        candidates: 读快照内取得的旧事实和稳定配对。

    Returns:
        按候选配对顺序排列的判定。

    Raises:
        InvalidStructuredOutput: 模型响应未精确覆盖送入的配对。
    """
    facts = {
        **candidates.existing_facts,
        **{item.fact.id: item.fact for item in prepared},
    }
    resolved: dict[FactPair, FactComparison] = {}
    pending: dict[str, FactPair] = {}
    for index, pair in enumerate(candidates.pairs, start=1):
        if intervals_disjoint(facts[pair[0]], facts[pair[1]]):
            resolved[pair] = FactComparison(pair, "coexists", disjoint=True)
        else:
            pending[str(index)] = pair

    if pending:
        prompt = _comparison_prompt(pending, facts)
        response = await llm.complete_json(
            system=_SYSTEM, prompt=prompt, schema=_SCHEMA
        )
        decisions = cast(_ComparisonOutput, response)["decisions"]
        by_id = {row["id"]: row["verdict"] for row in decisions}
        if len(decisions) != len(pending) or by_id.keys() != pending.keys():
            raise InvalidStructuredOutput("事实比较结果须恰好覆盖每个输入配对一次")
        for key, pair in pending.items():
            resolved[pair] = FactComparison(pair, by_id[key])
    return tuple(resolved[pair] for pair in candidates.pairs)


def _comparison_prompt(
    pairs: Mapping[str, FactPair], facts: Mapping[FactId, Fact]
) -> str:
    fact_ids = tuple(
        dict.fromkeys(fact_id for pair in pairs.values() for fact_id in pair)
    )
    fact_numbers = {fact_id: index for index, fact_id in enumerate(fact_ids, start=1)}
    entity_numbers: dict[EntityId, int] = {}
    rows = []
    for fact_id in fact_ids:
        fact = facts[fact_id]
        subject = entity_numbers.setdefault(
            fact.subject.entity_id, len(entity_numbers) + 1
        )
        if isinstance(fact.object, EntityRef):
            target: dict[str, object] = {
                "entity": entity_numbers.setdefault(
                    fact.object.entity_id, len(entity_numbers) + 1
                )
            }
        else:
            target = {"value": fact.object.value, "kind": fact.object.kind}
        rows.append(
            {
                "id": fact_numbers[fact_id],
                "subject": subject,
                "predicate": fact.predicate.value,
                "object": target,
                "text": fact.text,
                "valid_at": fact.valid_at,
                "invalid_at": fact.invalid_at,
            }
        )
    return json.dumps(
        {
            "facts": rows,
            "pairs": [
                {"id": key, "left": fact_numbers[left], "right": fact_numbers[right]}
                for key, (left, right) in pairs.items()
            ],
        },
        ensure_ascii=False,
    )
