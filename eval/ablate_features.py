"""针对最新 read-path 证据特性的离线算法消融。

这不是公开的 benchmark 结论。它是一个零密钥的冒烟证明：每个特性确实改变了目标证据行为——
recall 类特性会加入禁用后无法呈现的证据，precision 守卫会剔除已知坏证据而不丢失目标信号。

    python eval/ablate_features.py

真正可发布的门槛仍是 eval/bench.py 在 LongMemEval/LOCOMO 上带原始 JSONL 日志的评测。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from contextvars import ContextVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Config, MemoryEngine  # noqa: E402
from mnemotrace.types import Fact  # noqa: E402
from mnemotrace.util import DAY  # noqa: E402

_MODEL = ContextVar("ablation_model", default=None)


def _memory(**kwargs):
    return MemoryEngine(llm=_MODEL.get(), **kwargs)


BASE = 1_700_000_000.0


@dataclass(frozen=True)
class AblationResult:
    feature: str
    enabled_hit: bool
    disabled_hit: bool
    improved: bool
    enabled_tokens: int
    disabled_tokens: int
    enabled_latency_ms: float
    disabled_latency_ms: float
    target: str


def _tokens(text: str) -> int:
    return len(text.split())


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t0) * 1000.0


def _chain_context(enabled: bool) -> str:
    mem = _memory(config=Config(chain_evidence=enabled))
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE, text='Wei works at Tencent')
    mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY, text='Wei works at Moonshot AI')
    return mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )


def _digest_fallback_context(enabled: bool) -> str:
    mem = _memory(config=Config(digest_fallback=enabled))
    ep = mem.add(
        "To rotate the PAT: open security settings, regenerate the token, then update CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
        event_time=BASE,
    )
    ep.digest = ep.content
    mem._sync_episode(ep)
    res = mem.search("How do I rotate the PAT?", user_id="u1")
    return f"{res.via}\n{res.answer()}"


def _raw_context(enabled: bool) -> str:
    mem = _memory(config=Config(provenance_evidence=enabled))
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id], text='Apollo launch code A17',
    )
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    return mem.lean_context(
        "What is Apollo's launch code?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )


def _provenance_chunk_context(enabled: bool) -> str:
    mem = _memory(
        config=Config(
            provenance_evidence=False,
            provenance_chunk_promotion=enabled,
        )
    )
    source = mem.add(
        "A17 is written on the tag tucked inside the blue binder.",
        user_id="u1",
        session_id="apollo-source",
        event_time=BASE,
    )
    for i in range(5):
        mem.add(
            f"Apollo launch code rehearsal note {i}: the team reviewed old checklist formats.",
            user_id="u1",
            session_id=f"apollo-distractor-{i}",
            event_time=BASE + (i + 1) * DAY,
        )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[source.id], text='Apollo launch code A17',
    )
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    return mem.lean_context(
        "What is Apollo's launch code?",
        user_id="u1",
        persona=False,
        n_digests=0,
        n_chunks=1,
        char_budget=10_000,
    )


def _chain_provenance_context(enabled: bool) -> str:
    mem = _memory(
        config=Config(
            chain_evidence=enabled,
            provenance_evidence=False,
            provenance_chunk_promotion=True,
        )
    )
    mem.add(
        "Wei works at Tencent and keeps a blue employee badge.",
        user_id="u1",
        session_id="old-source",
        event_time=BASE,
    )
    mem.add(
        "Wei works at Moonshot AI on agent memory.",
        user_id="u1",
        session_id="new-source",
        event_time=BASE + 30 * DAY,
    )
    mem.consolidate()
    return mem.lean_context(
        "Where did Wei work before Moonshot AI?",
        user_id="u1",
        persona=False,
        n_digests=0,
        n_chunks=1,
        char_budget=10_000,
    )


def _evidence_budget_context(enabled: bool) -> str:
    mem = _memory(config=Config(evidence_budgeting=enabled))
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id], text='Apollo launch code A17',
    )
    mem.fact_store.upsert(fact.id, fact.embedding, fact)
    for i in range(12):
        mem.add_fact(
            "Apollo",
            "project_note",
            f"background filler note {i} with operational chatter that is not the checklist location",
            user_id="u1",
            valid_at=BASE + (i + 1) * DAY, text=' '.join(('Apollo', 'project note', f'background filler note {i} with operational chatter that is not the checklist location')),
        )
    return mem.lean_context(
        "What is Apollo's launch code and where is the printed checklist?",
        user_id="u1",
        persona=False,
        n_digests=0,
        n_chunks=0,
        char_budget=360,
    )


def _graph_context(enabled: bool) -> str:
    mem = _memory(config=Config(graph_proximity=enabled))
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, object_kind="entity", text='Wei colleague Lin')
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, object_kind="entity", text='Lin works at Moonshot AI')
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY, object_kind="entity", text='Moonshot AI based in Beijing')
    return mem.context_for("Tell me about Wei", user_id="u1", k_chunks=0, graph=True)


def _graph_relation_context(enabled: bool) -> str:
    mem = _memory(config=Config(graph_relation_awareness=enabled))
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, object_kind="entity", text='Wei colleague Lin')
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, object_kind="entity", text='Lin works at Moonshot AI')
    mem.add_fact("Lin", "likes", "jazz", user_id="u1", valid_at=BASE + 2 * DAY, object_kind="entity", text='Lin likes jazz')
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 3 * DAY, object_kind="entity", text='Moonshot AI based in Beijing')
    return mem.context_for("Where is Wei's colleague's company based?", user_id="u1", k_chunks=0, graph=True)


def _graph_reinforcement_context(enabled: bool) -> str:
    mem = _memory(config=Config(graph_path_reinforcement=enabled))
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, object_kind="entity", text='Wei colleague Lin')
    mem.add_fact("Wei", "mentor", "Maya", user_id="u1", valid_at=BASE + DAY, object_kind="entity", text='Wei mentor Maya')
    mem.add_fact("Lin", "works_on", "Atlas", user_id="u1", valid_at=BASE + 2 * DAY, object_kind="entity", text='Lin works on Atlas')
    mem.add_fact("Maya", "works_on", "Atlas", user_id="u1", valid_at=BASE + 3 * DAY, object_kind="entity", text='Maya works on Atlas')
    mem.add_fact("Lin", "works_on", "Zephyr", user_id="u1", valid_at=BASE + 4 * DAY, object_kind="entity", text='Lin works on Zephyr')
    mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + 5 * DAY, object_kind="entity", text='Atlas based in Reykjavik')
    mem.add_fact("Zephyr", "based_in", "Lisbon", user_id="u1", valid_at=BASE + 6 * DAY, object_kind="entity", text='Zephyr based in Lisbon')
    return mem.context_for("Where is Wei's project based?", user_id="u1", k_chunks=0, graph=True)


def _contains(marker: str, target: str):
    return lambda ctx: marker in ctx and target in ctx


def _before(target: str, distractor: str):
    def judge(ctx: str) -> bool:
        scope = ctx.split("RELATED FACTS (graph traversal):", 1)[-1]
        return target in scope and distractor in scope and scope.index(target) < scope.index(distractor)

    return judge


def _run_ablation() -> tuple[list[AblationResult], dict]:
    cases = [
        (
            "chain_evidence",
            _chain_context,
            _contains("FACT EVOLUTION", "Tencent"),
            "Tencent",
        ),
        (
            "digest_fallback",
            _digest_fallback_context,
            lambda answer: "digest" in answer and "security settings" in answer and "pat-runbook" in answer,
            "derived session digest answers fact-miss how-to query",
        ),
        (
            "provenance_evidence",
            _raw_context,
            _contains("PROVENANCE EVENT EVIDENCE (selected sources for retrieved facts):", "blue binder"),
            "blue binder",
        ),
        (
            "provenance_chunk_promotion",
            _provenance_chunk_context,
            _contains("RELEVANT CONVERSATIONS (full detail):", "blue binder"),
            "source episode promoted into full-detail raw chunk",
        ),
        (
            "chain_provenance_promotion",
            _chain_provenance_context,
            _contains("RELEVANT CONVERSATIONS (full detail):", "blue employee badge"),
            "superseded fact source promoted for previous-value questions",
        ),
        (
            "evidence_budgeting",
            _evidence_budget_context,
            lambda ctx: "blue binder" in ctx,
            "tight budget keeps exact raw evidence",
        ),
        (
            "graph_proximity",
            _graph_context,
            _contains("RELATED FACTS (graph traversal):", "Moonshot AI based in Beijing"),
            "Moonshot AI based in Beijing",
        ),
        (
            "graph_relation_awareness",
            _graph_relation_context,
            _before("Moonshot AI based in Beijing", "Lin likes jazz"),
            "target relation before same-node distractor",
        ),
        (
            "graph_path_reinforcement",
            _graph_reinforcement_context,
            _before("Atlas based in Reykjavik", "Zephyr based in Lisbon"),
            "multi-path target before single-path distractor",
        ),
    ]
    rows: list[AblationResult] = []
    for name, builder, judge, target in cases:
        enabled_ctx, enabled_lat = _timed(lambda b=builder: b(True))
        disabled_ctx, disabled_lat = _timed(lambda b=builder: b(False))
        enabled_hit = judge(enabled_ctx)
        disabled_hit = judge(disabled_ctx)
        rows.append(
            AblationResult(
                feature=name,
                enabled_hit=enabled_hit,
                disabled_hit=disabled_hit,
                improved=enabled_hit and not disabled_hit,
                enabled_tokens=_tokens(enabled_ctx),
                disabled_tokens=_tokens(disabled_ctx),
                enabled_latency_ms=enabled_lat,
                disabled_latency_ms=disabled_lat,
                target=target,
            )
        )
    summary = {
        "type": "summary",
        "n": len(rows),
        "improved": sum(1 for r in rows if r.improved),
        "enabled_hits": sum(1 for r in rows if r.enabled_hit),
        "disabled_hits": sum(1 for r in rows if r.disabled_hit),
    }
    return rows, summary


def run_ablation(*, llm):
    from mnemotrace.extraction import require_llm
    require_llm(llm)
    token = _MODEL.set(llm)
    try:
        return _run_ablation()
    finally:
        _MODEL.reset(token)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run zero-key feature ablations for MnemoTrace read-path evidence.")
    ap.add_argument("--jsonl", help="write per-feature rows plus a summary as JSONL")
    ap.add_argument("--llm", help="model name, or set MNEMOTRACE_LLM")
    args = ap.parse_args()
    from mnemotrace.llm.providers import make_llm, load_dotenv
    load_dotenv()
    model = args.llm or os.environ.get("MNEMOTRACE_LLM", "")
    if not model:
        ap.error("configure --llm or MNEMOTRACE_LLM")

    rows, summary = run_ablation(llm=make_llm(model))
    print("MnemoTrace feature ablation smoke -- explicit LLM; synthetic smoke only")
    print("feature                  enabled  disabled  improved  target")
    print("-" * 78)
    for r in rows:
        print(
            f"{r.feature:24} {'HIT' if r.enabled_hit else '--':7} "
            f"{'HIT' if r.disabled_hit else '--':8} {'YES' if r.improved else 'NO':8} {r.target}"
        )
    print("-" * 78)
    print(f"improved {summary['improved']}/{summary['n']} features")

    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r.__dict__, ensure_ascii=False, sort_keys=True) + "\n")
            fh.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"raw ablation log written to {args.jsonl}")


if __name__ == "__main__":
    main()
