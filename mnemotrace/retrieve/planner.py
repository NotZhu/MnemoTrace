"""Multi-hop 查询规划器(CLAUDE.md Bet B)—— 全领域的软肋,我们的主攻目标。

把关系型问题("Where does Wei's colleague work?")分解成有序谓词链 [colleague, works_at],锚定在
一个实体(Wei)上,然后逐跳遍历双时间轴图。只对真正的 multi-hop 问题触发(≥2 个谓词 + 已知锚点);
单跳问题直接落到 hybrid 检索器。

分解在同一个 `.plan()` API 后有两个后端:
  * 有 LLM -> 由模型挑选锚点 + 有序谓词链,但只能从该用户存储中实际存在的谓词和实体里选
    (因此无法凭空捏造边)。这把规划器推广到下面的手工关键词映射之外 —— 这是对任意 multi-hop
    表述的关键收益。
  * 离线 -> 确定性关键词映射(`_PRED_KEYWORDS`)及其 relation/location 链扩展,让零依赖
    demo/测试无需模型也能规划。这条路径保持不变。
无论哪种方式,图遍历完全一致;链断了返回 None,search() 便回退到 hybrid。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import re

from ..config import Config
from ..store import GraphStore, VectorStore
from ..types import Fact
from .jsonio import loads_object
from .lexical import stem, stems

# 查询关键词 -> 图谓词
_PRED_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("colleague", "coworker", "co-worker"), "colleague"),
    (("sister",), "sister"),
    (("brother",), "brother"),
    (("mother", "mom"), "mother"),
    (("father", "dad"), "father"),
    (("parent",), "parent"),
    (("child",), "child"),
    (("spouse", "wife", "husband", "partner"), "spouse"),
    (("project", "projects", "initiative", "initiatives", "works_on", "worked_on"), "works_on"),
    (("work", "works", "working", "employer", "company", "job", "employed"), "works_at"),
    (("profession", "occupation", "role", "title"), "occupation"),
    (("live", "lives", "living", "city", "reside", "resides"), "lives_in"),
    (("based", "located", "location", "headquarters", "headquarter", "hq"), "based_in"),
]
_RELATION_PREDS = {"colleague", "sister", "brother", "mother", "father", "parent", "child", "spouse", "works_on"}
_ANSWER_ATTR_PREDS = {"works_at", "occupation", "lives_in", "based_in"}
_LOCATION_ATTR_PREDS = {"based_in", "located_in", "headquartered_in"}
_PROJECT_REL_PREDS = {"works_on", "project", "worked_on", "built", "building"}

# 规划器 LLM 拿到的是该用户真实的谓词/实体词表,且只能在这些之间成链 —— 以此接地,
# 确保它返回的是可遍历的边,而不是听起来合理的幻觉。
_PLAN_SYSTEM = (
    "You decompose a multi-hop question about a user's memory into a graph walk. "
    'Output ONLY JSON: {"anchor": <one entity name from the list, or "">, '
    '"predicates": [<ordered predicate names from the list — the chain of edges to follow from the anchor>]}. '
    "Use ONLY names and predicates from the provided lists; never invent one. If the question needs fewer "
    'than two hops or has no anchor entity in the list, return {"anchor":"","predicates":[]}.'
)
_MAX_VOCAB = 60  # 限制喂给规划器 LLM 的谓词/实体列表大小(保持 prompt 精简)


@dataclass
class PlanResult:
    answer: str
    facts: list[Fact] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)


class MultiHopPlanner:
    def __init__(
        self,
        graph: GraphStore,
        fact_store: VectorStore,
        config: Config,
        extra_fact_stores: Optional[list[VectorStore]] = None,
        llm=None,
    ) -> None:
        self.graph = graph
        self.fact_store = fact_store
        self.fact_stores = [fact_store] + list(extra_fact_stores or [])
        self.config = config
        # 可开关,让 LLM 分解像其他读路径特性一样可消融:它会在读路径上增加一次 LLM 调用,
        # 因此其成本/延迟/精度的取舍必须可度量,而不是隐式的。
        self.llm = llm if getattr(config, "planner_llm_decomposition", True) else None

    def _fact(self, fact_id: str) -> Optional[Fact]:
        for store in self.fact_stores:
            fact = store.get(fact_id)
            if fact is not None:
                return fact
        return None

    def _ordered_predicates(self, query: str) -> list[str]:
        toks = stems(query)
        tokset = set(toks)
        hits: list[tuple[int, str]] = []
        for keywords, pred in _PRED_KEYWORDS:
            positions = [toks.index(stem(kw)) for kw in keywords if stem(kw) in tokset]
            if positions:
                hits.append((min(positions), pred))
        hits.sort()
        # 去重并保持顺序
        seen: set[str] = set()
        ordered: list[str] = []
        for _, pred in hits:
            if pred not in seen:
                ordered.append(pred)
                seen.add(pred)
        if not self.config.planner_project_chains:
            ordered = [p for p in ordered if p not in _PROJECT_REL_PREDS]
        if not self.config.planner_location_chains:
            ordered = [p for p in ordered if p not in _LOCATION_ATTR_PREDS]
        rels = [p for p in ordered if p in _RELATION_PREDS]
        attrs = [p for p in ordered if p in _ANSWER_ATTR_PREDS]
        if rels and attrs:
            # 领属关系问题("user's sister's profession")在英文里先提到目标属性、后提到关系,
            # 但图遍历必须从 user -> 关系开始。
            ordered = rels + [p for p in attrs if p not in rels]
        return ordered

    def _anchor_entity(self, query: str, user_id: str):
        if re.search(r"\b(my|user's|the user's|their|his|her)\b", query.lower()):
            return self.graph.get_entity(user_id, user_id) or self.graph.get_entity(user_id, "user")
        q = set(stems(query))
        for ent in self.graph.entities.values():
            if ent.user_id != user_id:
                continue
            name_toks = stems(ent.name)
            if name_toks and all(t in q for t in name_toks):
                return ent
        return None

    def _location_constraint(self, query: str) -> str:
        q = query.lower()
        m = re.search(r"\b(?:moved|relocated|lives?|living)\s+(?:to|in)\s+([a-z][a-z' -]{1,40})", q)
        if not m:
            return ""
        return re.split(r"\b(?:for|with|after|before|who|that|and|or)\b|[?.!,;:]", m.group(1), 1)[0].strip()

    def _location_fact(self, entity_id: str, target: str, as_of: Optional[float]) -> Optional[Fact]:
        if not target:
            return None
        target_terms = set(stems(target))
        if not target_terms:
            return None
        for rel in self.graph.neighbors(entity_id, as_of, "out"):
            if rel.predicate != "lives_in":
                continue
            obj = self.graph.entities.get(rel.object_id)
            name = obj.name if obj else ""
            if target_terms <= set(stems(name)):
                fact = self._fact(rel.fact_id)
                if fact is not None and fact.is_live(as_of):
                    return fact
        return None

    @staticmethod
    def _pred_matches(pred: str, rel_pred: str) -> bool:
        """边按精确名称或其谓词族匹配规划的谓词(works_at/works_on/based_in/spouse)。"""
        return (
            rel_pred == pred
            or (pred == "works_at" and rel_pred.startswith("work"))
            or (pred == "works_on" and rel_pred in _PROJECT_REL_PREDS)
            or (pred == "based_in" and rel_pred in _LOCATION_ATTR_PREDS)
            or (pred == "spouse" and rel_pred in {"wife", "husband", "partner"})
        )

    def _user_predicates(self, user_id: str) -> list[str]:
        """该用户 live fact 中实际出现的去重谓词 —— LLM 规划器唯一允许成链的边
        (接地使其无法幻觉出关系)。"""
        preds: list[str] = []
        seen: set[str] = set()
        for store in self.fact_stores:
            for f in store.values():
                if f.user_id != user_id or not f.is_live():
                    continue
                if f.predicate and f.predicate not in seen:
                    seen.add(f.predicate)
                    preds.append(f.predicate)
                if len(preds) >= _MAX_VOCAB:
                    return preds
        return preds

    def _user_entities(self, user_id: str) -> list[str]:
        names: list[str] = []
        for ent in self.graph.entities.values():
            if ent.user_id == user_id and ent.name:
                names.append(ent.name)
            if len(names) >= _MAX_VOCAB:
                break
        return names

    def _llm_plan(self, query: str, user_id: str):
        """让 LLM 给出 {anchor, predicates},约束在该用户的真实词表内。返回 (anchor_entity, predicates):
        锚点对图解析;谓词过滤为真实存在的(捏造的边被丢弃)。任何解析/校验失败都返回 (None, []),
        调用方从而回退到关键词分解器 —— 模型出问题绝不破坏检索。"""
        allowed = self._user_predicates(user_id)
        if len(allowed) < 2:
            return None, []  # 关系结构不足,无法成链
        prompt = f"Question: {query}\nEntities: {self._user_entities(user_id)}\nPredicates: {allowed}\nJSON:"
        try:
            raw = self.llm.complete(prompt, system=_PLAN_SYSTEM)
        except Exception:  # noqa: BLE001 -- 绝不让模型错误破坏读路径
            return None, []
        obj = loads_object(raw)
        if not obj:
            return None, []
        allowed_set = {p.lower() for p in allowed}
        preds = [str(p) for p in (obj.get("predicates") or []) if str(p).lower() in allowed_set]
        anchor_name = str(obj.get("anchor") or "").strip()
        anchor = self.graph.get_entity(user_id, anchor_name) if anchor_name else None
        return anchor, preds

    def _make_plan(self, query: str, user_id: str):
        """有 LLM 且结果可信时用 LLM 分解,否则用确定性关键词映射(含 relation/location 链扩展)
        —— 离线行为保持不变。"""
        if self.llm is not None:
            anchor, preds = self._llm_plan(query, user_id)
            if anchor is not None and len(preds) >= 2:
                return anchor, preds
        return self._anchor_entity(query, user_id), self._ordered_predicates(query)

    def _bridge_paths(
        self, frontier_paths: dict, pred: str, user_id: str, as_of: Optional[float]
    ) -> dict:
        """没有图 EDGE 匹配 `pred` 时的回退跳:以前沿实体为主语,直接在 fact 存储中查该谓词。
        挽回那些存在但从未变成干净图边的 fact(规划器与普通检索之间的接缝)。仅限 LLM 路径 ——
        离线关键词遍历保持与原来逐字节一致。"""
        names = {
            self.graph.entities[eid].name.lower(): eid
            for eid in frontier_paths
            if eid in self.graph.entities
        }
        out: dict[str, list[Fact]] = {}
        for store in self.fact_stores:
            for f in store.values():
                if f.user_id != user_id or not f.is_live(as_of):
                    continue
                eid = names.get(f.subject.lower())
                if eid is None or not self._pred_matches(pred, f.predicate):
                    continue
                obj_ent = self.graph.get_entity(user_id, f.object)
                if obj_ent is not None:
                    out[obj_ent.id] = frontier_paths[eid] + [f]
        return out

    def plan(self, query: str, user_id: str, as_of: Optional[float] = None) -> Optional[PlanResult]:
        anchor, preds = self._make_plan(query, user_id)
        if anchor is None or len(preds) < 2:
            return None  # 不是有把握的 multi-hop 问题;交给 hybrid 处理

        frontier_paths: dict[str, list[Fact]] = {anchor.id: []}
        location = self._location_constraint(query)
        for pred in preds:
            if pred in _ANSWER_ATTR_PREDS and location and len(preds) > 1:
                constrained: dict[str, list[Fact]] = {}
                for eid, facts in frontier_paths.items():
                    loc_fact = self._location_fact(eid, location, as_of)
                    if loc_fact is not None:
                        constrained[eid] = facts + [loc_fact]
                if not constrained:
                    return None
                frontier_paths = constrained

            next_paths: dict[str, list[Fact]] = {}
            for eid, facts in frontier_paths.items():
                for rel in self.graph.neighbors(eid, as_of, "out"):
                    if self._pred_matches(pred, rel.predicate):
                        fact = self._fact(rel.fact_id)
                        if fact is None or not fact.is_live(as_of):
                            continue
                        next_paths[rel.object_id] = facts + ([fact] if fact is not None else [])
            if not next_paths and self.llm is not None:
                # 本跳没有图边 —— 放弃前先试 fact-store 桥接(仅限 LLM 路径):
                # 一个存在但从未变成干净边的 fact 仍可能补完这条链。
                next_paths = self._bridge_paths(frontier_paths, pred, user_id, as_of)
            if not next_paths:
                return None  # 链断了 -> 没有把握的 multi-hop 答案
            frontier_paths = next_paths

        answer_names = [self.graph.entities[eid].name for eid in frontier_paths if eid in self.graph.entities]
        if not answer_names:
            return None
        answer_id = next(eid for eid in frontier_paths if eid in self.graph.entities)
        return PlanResult(answer=self.graph.entities[answer_id].name, facts=frontier_paths[answer_id], chain=preds)
