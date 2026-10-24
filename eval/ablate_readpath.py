"""在真实 benchmark 上做逐特性 read-path 消融，测量时不用 answerer 也不用 judge。

为什么不是直接对每个特性跑 eval/bench.py --ablate：QA 分数被 answerer 的方差主导（标准 rig 上
逐次波动约 ±6-10 题/500），超过大多数单特性本身的影响。经 QA 路径消融 25+ 个开关，测到的多半
是 answerer 的心情，成本还是 3 倍。

所以这里测量的是 read-path 特性真正控制的东西：**金标准答案是否存活在检索到的上下文里？**
检索从未交给 answerer 的内容，answerer 不可能答出，因此 answer-in-context 是这些特性所负责的
必要条件——而且不需要 answerer 也不需要 judge，更便宜也远更稳定。（这只是必要条件而非充分
条件：特性可以把答案放进上下文，answerer 仍可能错过。这里出现的任何有趣信号，先升级到真正的
eval/bench.py 运行再相信头条差值。）

    python eval/ablate_readpath.py --limit 60 --extractor volcano:doubao-seed-1-6-flash-250615 \
        --out .artifacts/eval/readpath_ablation.jsonl

每个特性都以"基线减去该特性"的方式运行，所以 delta 是该特性在其余一切之上的边际贡献——
不是它在孤立状态下的价值。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Memory  # noqa: E402
from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm  # noqa: E402
from eval.bench import mnemotrace_config  # noqa: E402
from eval.longmemeval import ingest, load_data  # noqa: E402

# mnemotrace_config() 能关掉的每一个 read-path 开关，按主题分组，让报告读起来是一个个主题，
# 而不是 25 个不分青红皂白的布尔值。
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "graph / multi-hop": (
        "graph_proximity", "graph_relation_awareness", "graph_path_reinforcement",
        "graph_self_anchor", "graph_entity_alias_anchor", "graph_negative_constraints",
        "planner_location_chains", "planner_project_chains", "planner_llm_decomposition",
    ),
    "evidence planning": ("evidence_planner", "evidence_budgeting"),
    "temporal / chains": (
        "chain_evidence", "temporal_history_queries",
        "provenance_chunk_promotion", "provenance_evidence",
    ),
    "preference extraction": (
        "explicit_preference_extraction", "preference_object_filter",
        "preference_object_normalization", "preference_reversal_extraction",
    ),
    "aggregation": (
        "numeric_aggregation_candidates", "aggregation_recall_expansion",
        "aggregation_constraint_filter",
    ),
    "derived layers": ("summary_fallback", "procedural_memory", "procedural_extraction"),
}
ALL_FEATURES = tuple(f for group in FEATURE_GROUPS.values() for f in group)


def _norm(s: str) -> str:
    """宽松匹配：answerer 看到的是上下文，不是精确匹配的评分器，所以大小写/标点和内部
    空白不应决定答案"是否在场"。"""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def answer_in_context(context: str, gold: str) -> bool:
    """金标准答案能否从检索到的上下文中找回。

    短的金标准答案（'Nike'、'55-inch'）整体匹配。长的金标准答案是句子，要求逐字匹配会漏掉
    那些明显换了措辞携带事实的上下文；后者改用内容词覆盖率计分（>=80% 的 >3 字符 token 出现）。
    """
    ctx, g = _norm(context), _norm(gold)
    if not g:
        return False
    if g in ctx:
        return True
    toks = [t for t in g.split() if len(t) > 3]
    if not toks:
        return False
    hit = sum(1 for t in toks if t in ctx)
    return hit / len(toks) >= 0.8


def context_for_item(item: dict, embedder, extractor, ablations: tuple[str, ...],
                     topk: int, chunks: int, extract_k: int) -> tuple[str, int]:
    qid, q = item["question_id"], item["question"]
    mem = Memory(config=mnemotrace_config(ablations=ablations), embedder=embedder, llm=extractor)
    ingest(mem, item, qid)
    if extract_k > 0:
        mem.engine.consolidate(mem.retrieve_episodes(q, qid, extract_k))
    else:
        mem.consolidate()
    ctx = mem.context_for(q, user_id=qid, top_k=topk, k_chunks=chunks)
    return ctx, len(ctx) // 4  # 粗略的 token 估算，只用于相对成本一列


def run_variant(items, embedder, extractor, ablations, args, label) -> dict:
    hits = 0
    tokens = 0
    errors = 0
    t0 = time.time()

    def one(item):
        try:
            ctx, tok = context_for_item(item, embedder, extractor, ablations,
                                        args.topk, args.chunks, args.extract_k)
            return answer_in_context(ctx, item.get("answer", "")), tok, None
        except Exception as exc:  # noqa: BLE001 — 一个坏条目不能毁掉整轮扫描
            return False, 0, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for ok, tok, err in pool.map(one, items):
            hits += int(ok)
            tokens += tok
            errors += int(err is not None)
    n = len(items)
    return {"label": label, "ablated": list(ablations), "answer_in_context": hits,
            "n": n, "rate": hits / n if n else 0.0,
            "avg_tokens": tokens // n if n else 0, "errors": errors,
            "elapsed_s": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="s")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--extractor", default="volcano:doubao-seed-1-6-flash-250615")
    ap.add_argument("--embedder", default="bge-small")
    ap.add_argument("--topk", type=int, default=15)
    ap.add_argument("--chunks", type=int, default=2)
    ap.add_argument("--extract-k", type=int, default=8)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--features", default="", help="comma-separated subset (default: all)")
    ap.add_argument("--repeat-baseline", type=int, default=2,
                    help="re-run the baseline N times to measure this rig's own noise floor")
    ap.add_argument("--out", default=".artifacts/eval/readpath_ablation.jsonl")
    args = ap.parse_args()

    load_dotenv()
    items = load_data(args.data)[:args.limit]
    items = [it for it in items if it.get("answer")]
    embedder = make_embedder(args.embedder)
    extractor = make_llm(args.extractor)
    features = tuple(f.strip() for f in args.features.split(",") if f.strip()) or ALL_FEATURES

    print(f"READ-PATH ABLATION | {len(items)} items | {len(features)} features")
    print(f"  metric=answer_in_context (no answerer, no judge)  extractor={args.extractor}\n")

    rows = []
    # 基线重复运行：相同运行之间的波动就是噪声底线。特性 delta 小于这个区间的，什么也
    # 证明不了。
    baselines = []
    for i in range(max(1, args.repeat_baseline)):
        b = run_variant(items, embedder, extractor, (), args, f"baseline#{i+1}")
        baselines.append(b)
        rows.append(b)
        print(f"  baseline#{i+1}: {b['answer_in_context']}/{b['n']} = {b['rate']*100:.1f}%"
              f"  ({b['elapsed_s']}s)")
    base_rate = sum(b["rate"] for b in baselines) / len(baselines)
    noise = (max(b["rate"] for b in baselines) - min(b["rate"] for b in baselines)) if len(baselines) > 1 else 0.0
    print(f"  baseline mean {base_rate*100:.1f}%   noise band ±{noise*100:.1f} pp\n")

    for f in features:
        r = run_variant(items, embedder, extractor, (f,), args, f"-{f}")
        r["delta_pp"] = round((base_rate - r["rate"]) * 100, 1)  # 正值 => 特性有帮助
        rows.append(r)
        verdict = "helps" if r["delta_pp"] > noise * 100 else ("hurts" if r["delta_pp"] < -noise * 100 else "noise")
        print(f"  -{f:<34} {r['rate']*100:5.1f}%   delta {r['delta_pp']:+5.1f} pp   [{verdict}]")

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as fh:
            fh.write(json.dumps({"kind": "readpath_ablation_meta", "items": len(items),
                                 "baseline_rate": base_rate, "noise_band_pp": noise * 100,
                                 "extractor": args.extractor, "embedder": args.embedder,
                                 "topk": args.topk, "chunks": args.chunks,
                                 "extract_k": args.extract_k}) + "\n")
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
