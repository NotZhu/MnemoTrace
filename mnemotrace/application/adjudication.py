"""准备待比较的事实，并在事务外批量裁决。"""

import asyncio
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from heapq import heappush, heappushpop
from itertools import batched, combinations
from math import fsum, hypot
from typing import Literal, NamedTuple, TypedDict, cast

from ..domain.entity import Entity
from ..domain.enums import Source
from ..domain.fact import Fact, intervals_disjoint
from ..domain.ids import EntityId, FactId, SpaceId
from ..domain.values import EntityRef, LiteralValue, Predicate
from ..ports.inference import LLM, Embedder, InvalidStructuredOutput
from ..ports.queries import FactFilter, FactQuery, FactSlot
from ..ports.runtime import IdGenerator
from ..ports.storage import SpaceReader
from .extraction import EntityMention, ExtractedFact, SpaceUserMention, TimeEvidence

_OLD_CANDIDATE_LIMIT = 10
_RRF_OFFSET = 60
_BATCH_PEER_LIMIT = 10
_VECTOR_MIN_COSINE = 0.60
_BATCH_MIN_COSINE = 0.60
_COMPARISON_PAIR_LIMIT = 30
_COMPARISON_CONCURRENCY = 4

type FactPair = tuple[FactId, FactId]
type BatchPairs = tuple[tuple[FactPair, ...], ...]
type Verdict = Literal["duplicate", "coexists", "conflict", "unknown"]

_SYSTEM = """判断 pairs 中指定的事实对。facts 是待分析的数据。每对在 decisions 中返回恰好一个结果，原样填写配对 id。verdict 只取 duplicate、coexists、conflict、unknown。

一、先确认两条事实各自说了什么

结合正文、参与者、角色和关系含义，核对属性、条件、否定、适用范围和关键值。区分计划、已发生的事件、持续状态及陈述归属。不同谓词或正反句式可以表达同一关系。客体类型存在差异时，仍可依据正文比较明确表达的设置取值。
subject 和 object.entity 共用实体编号。同号指向同一个已绑定实体。编号不同时按各自身份理解，同名不足以确认身份相同。

二、选一个判定

- duplicate：两条表达同一断言，参与者及角色、条件、范围、肯定或否定、关键值和事件实例一致。不同时间确认同一个持续状态，可以判重复。
- coexists：两条明确无关，或可以同时成立。不同属性、不同适用范围、允许同时存在的多个取值、另一次发生的事件都属于此类。
- conflict：两条在同一适用范围内明确互斥，例如明确终止、角色交接，或同一个单值设置出现互斥取值。旧状态的 invalid_at 为 null 时，同一范围内明确改用另一取值仍判冲突。
- unknown：两条可能在说同一项事实，但身份、角色或适用范围不足以确定关系。

三、按事实给出的范围理解时间

历史陈述只约束它所描述的时段。相似措辞、不同日期或单纯更新近都不足以判冲突。旧事实是否已截止，依据旧事实自己的有效区间。有效期先后、证据覆盖和实际截止由后端计算。

例如，旧事实「用户上班主要坐地铁」没有已知截止时间，与「用户现在上班主要改为骑车」判 conflict。
如果后一条只说「用户现在周末出门主要骑车」，两条判 coexists。
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


class _ComparisonInput(NamedTuple):
    text: str
    subject: EntityRef
    predicate: Predicate
    object: EntityRef | LiteralValue
    valid_at: float | None
    invalid_at: float | None


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
            profile_groups=item.profile_groups,
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
    batch_pairs: BatchPairs | None = None,
) -> ComparisonCandidates:
    """用同一快照读取旧事实并生成新旧和批内比较对。

    Args:
        reader: 绑定已授权空间的读快照或提交复核会话。
        prepared: 本批已绑定和嵌入的候选。
        embedding_space: 与候选向量和索引匹配的模型标识。
        reference_time: 本次写入共用的请求时刻。
        batch_pairs: 按 prepared 顺序分组的批内配对，写入与复核可共用。

    Returns:
        命中的旧事实和按裁决顺序排列的去重配对。
    """
    existing: dict[FactId, Fact] = {}
    old_candidate_ids: list[list[FactId]] = []
    for item in prepared:
        fact = item.fact
        filters = FactFilter(
            state="all",
            overlap_start=fact.valid_at,
            overlap_end=fact.invalid_at,
        )
        same_slot = replace(filters, subject=fact.subject, predicate=fact.predicate)
        same_slot_probe = await reader.query_facts(
            FactQuery(filters=same_slot, limit=_OLD_CANDIDATE_LIMIT + 1),
            reference_time=reference_time,
        )
        same_ids = tuple(old.id for old in same_slot_probe)
        if len(same_ids) > _OLD_CANDIDATE_LIMIT:
            ranked = await _rank_old_candidates(
                reader,
                item,
                same_slot,
                embedding_space=embedding_space,
                reference_time=reference_time,
            )
            # 同槽排序命中不足时保留无向量或词法不匹配的结构候选。
            same_ids = tuple(dict.fromkeys((*ranked, *same_ids)))[:_OLD_CANDIDATE_LIMIT]
        existing.update((old.id, old) for old in same_slot_probe if old.id in same_ids)

        cross_ids = await _rank_old_candidates(
            reader,
            item,
            replace(filters, exclude_slot=_slot(fact)),
            embedding_space=embedding_space,
            reference_time=reference_time,
        )
        old_candidate_ids.append(
            sorted({*same_ids, *cross_ids}, key=lambda fact_id: fact_id.value)
        )

    # 补齐排序入选但探查时尚未读取的旧事实。
    missing = dict.fromkeys(
        fact_id
        for ids in old_candidate_ids
        for fact_id in ids
        if fact_id not in existing
    )
    if missing:
        existing.update(await reader.get_facts(tuple(missing)))
    if batch_pairs is None:
        batch_pairs = build_batch_pairs(prepared)
    pairs: list[FactPair] = []
    for item, old_ids, new_pairs in zip(
        prepared, old_candidate_ids, batch_pairs, strict=True
    ):
        pairs.extend((item.fact.id, old_id) for old_id in old_ids)
        pairs.extend(new_pairs)
    return ComparisonCandidates(existing, tuple(pairs))


async def _rank_old_candidates(
    reader: SpaceReader,
    item: PreparedFact,
    filters: FactFilter,
    *,
    embedding_space: str,
    reference_time: float,
) -> tuple[FactId, ...]:
    lexical = await reader.search_lexical(
        item.fact.text,
        _OLD_CANDIDATE_LIMIT,
        filters=filters,
        reference_time=reference_time,
    )
    vectors = await reader.search_vectors(
        item.vector,
        _OLD_CANDIDATE_LIMIT,
        filters=filters,
        reference_time=reference_time,
        embedding_space=embedding_space,
        min_score=_VECTOR_MIN_COSINE,
    )
    scores: dict[FactId, float] = defaultdict(float)
    for channel in (lexical, vectors):
        for rank, (fact_id, _) in enumerate(channel, start=1):
            scores[fact_id] += 1 / (_RRF_OFFSET + rank)
    ranked = sorted(scores, key=lambda fact_id: (-scores[fact_id], fact_id.value))
    return tuple(ranked[:_OLD_CANDIDATE_LIMIT])


def build_batch_pairs(prepared: Sequence[PreparedFact]) -> BatchPairs:
    """计算可在同次写入初查和复核之间复用的批内配对。

    Args:
        prepared: 按抽取顺序排列且在写入期间固定的新事实候选。

    Returns:
        按左侧候选的输入顺序分组，组内按右侧候选的输入顺序排列。
    """
    if len(prepared) < 2:
        return ((),) * len(prepared)
    pairs: list[set[int]] = [set() for _ in prepared]
    neighbors: list[list[tuple[float, int]]] = [[] for _ in prepared]
    vectors = [_unit_vector(item.vector) for item in prepared]
    slots = [_slot(item.fact) for item in prepared]
    for left, right in combinations(range(len(prepared)), 2):
        if intervals_disjoint(prepared[left].fact, prepared[right].fact):
            continue
        if slots[left] == slots[right]:
            pairs[left].add(right)
            continue
        score = fsum(x * y for x, y in zip(vectors[left], vectors[right], strict=True))
        if score < _BATCH_MIN_COSINE:
            continue
        for index, peer in ((left, right), (right, left)):
            # 堆顶是最弱候选。同分时优先保留输入下标更小的一条。
            rank = (score, -peer)
            if len(neighbors[index]) < _BATCH_PEER_LIMIT:
                heappush(neighbors[index], rank)
            else:
                heappushpop(neighbors[index], rank)
    for index, peers in enumerate(neighbors):
        for _, neg_peer in peers:
            left, right = sorted((index, -neg_peer))
            pairs[left].add(right)
    return tuple(
        tuple((item.fact.id, prepared[peer].fact.id) for peer in sorted(peers))
        for item, peers in zip(prepared, pairs, strict=True)
    )


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
    *,
    reusable: Mapping[FactPair, FactComparison] | None = None,
) -> tuple[FactComparison, ...]:
    """按左侧新事实分片并发裁决，保留原配对顺序。

    Args:
        llm: 已装配的结构化模型端口。
        prepared: 本批已绑定的事实候选。
        candidates: 读快照内取得的旧事实和稳定配对。
        reusable: 已针对当前配对复核、可以沿用的判定。

    Returns:
        按候选配对顺序排列的判定。

    Raises:
        InvalidStructuredOutput: 模型响应未精确覆盖送入的配对。
    """
    facts = _comparison_facts(prepared, candidates)
    cached = {frozenset(pair): decision for pair, decision in (reusable or {}).items()}
    resolved = _resolve_comparisons(candidates.pairs, facts, cached)
    by_anchor: dict[FactId, list[tuple[str, FactPair]]] = defaultdict(list)
    for index, pair in enumerate(candidates.pairs, start=1):
        if pair not in resolved:
            by_anchor[pair[0]].append((str(index), pair))

    shards = (
        dict(shard)
        for pairs in by_anchor.values()
        for shard in batched(pairs, _COMPARISON_PAIR_LIMIT)
    )
    for wave in batched(shards, _COMPARISON_CONCURRENCY):
        tasks = [
            asyncio.create_task(_compare_shard(llm, shard, facts)) for shard in wave
        ]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                # 调用方取消时，gather 已发送取消，重复取消可能打断清理。
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        for result in results:
            resolved.update(result)
    return tuple(resolved[pair] for pair in candidates.pairs)


async def _compare_shard(
    llm: LLM, pairs: Mapping[str, FactPair], facts: Mapping[FactId, Fact]
) -> dict[FactPair, FactComparison]:
    response = await llm.complete_json(
        system=_SYSTEM, prompt=_comparison_prompt(pairs, facts), schema=_SCHEMA
    )
    decisions = cast(_ComparisonOutput, response)["decisions"]
    by_id = {row["id"]: row["verdict"] for row in decisions}
    if len(decisions) != len(pairs) or by_id.keys() != pairs.keys():
        raise InvalidStructuredOutput("事实比较结果须恰好覆盖每个输入配对一次")
    return {pair: FactComparison(pair, by_id[key]) for key, pair in pairs.items()}


def resolve_known_comparisons(
    prepared: Sequence[PreparedFact],
    current: ComparisonCandidates,
    previous: ComparisonCandidates,
    comparisons: Sequence[FactComparison],
) -> dict[FactPair, FactComparison]:
    """复用配对依据未变的判定，并直接判断有效期错开的当前配对。

    Args:
        prepared: 两次复核共用的本批候选。
        current: 新读快照内重查的比较候选。
        previous: 上次裁决使用的候选快照。
        comparisons: 基于上次快照取得的判定。

    Returns:
        当前配对中已能确定的判定，缺项须在写锁外请求模型。
    """
    changed = {
        fact_id
        for fact_id, fact in current.existing_facts.items()
        if fact_id not in previous.existing_facts
        or _comparison_input(fact)
        != _comparison_input(previous.existing_facts[fact_id])
    }
    reusable = {
        frozenset(comparison.pair): comparison
        for comparison in comparisons
        if changed.isdisjoint(comparison.pair)
    }
    return _resolve_comparisons(
        current.pairs, _comparison_facts(prepared, current), reusable
    )


def _comparison_input(fact: Fact) -> _ComparisonInput:
    # 只有送入模型的字段决定是否重判，提交时另用锁内最新证据和敏感标记。
    return _ComparisonInput(
        fact.text,
        fact.subject,
        fact.predicate,
        fact.object,
        fact.valid_at,
        fact.invalid_at,
    )


def _comparison_facts(
    prepared: Sequence[PreparedFact], candidates: ComparisonCandidates
) -> dict[FactId, Fact]:
    return {
        **candidates.existing_facts,
        **{item.fact.id: item.fact for item in prepared},
    }


def _resolve_comparisons(
    pairs: Sequence[FactPair],
    facts: Mapping[FactId, Fact],
    reusable: Mapping[frozenset[FactId], FactComparison],
) -> dict[FactPair, FactComparison]:
    resolved: dict[FactPair, FactComparison] = {}
    for pair in pairs:
        if intervals_disjoint(facts[pair[0]], facts[pair[1]]):
            resolved[pair] = FactComparison(pair, "coexists", disjoint=True)
        elif (known := reusable.get(frozenset(pair))) is not None:
            resolved[pair] = known if known.pair == pair else replace(known, pair=pair)
    return resolved


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
        fact = _comparison_input(facts[fact_id])
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
