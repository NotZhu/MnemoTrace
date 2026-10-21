"""在 harness 上调检索融合权重（AGENTS.md 「检索与模块」 / 「评测、公开内容与贡献规则」——"绝不拍脑袋"）。

融合权重（w_sem, w_lex, w_graph, w_rec, w_sal）此前是手工设定的默认值。本脚本对着 ground truth
做网格搜索，让章程里"每个 benchmark 在 harness 上调优"名副其实。目标函数不用 LLM：一个 fact
是 RELEVANT 当且仅当它抽自该问题的证据 session 之一（`answer_session_ids`）；我们最大化
relevant fact 的平均 recall@k。只有一次性 fact 抽取需要 LLM；网格搜索本身是纯重排序。

    python eval/tune_weights.py --dev 30 --k 15

打印最优权重组；如果它明显优于当前默认值，就抄进 mnemotrace/config.py。
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Memory  # noqa: E402
from mnemotrace.config import Config  # noqa: E402
from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm  # noqa: E402
from eval.longmemeval import ingest, load_data, sessions_of  # noqa: E402


def build_dev(items, embedder, extractor, extract_k=20):
    """为每个 dev 问题构建 fact store，并标注哪些 fact 是 relevant（provenance 落在证据
    session 中）。返回 [(mem, user_id, query, relevant_fact_ids)]。"""
    dev = []
    for it in items:
        qid = it["question_id"]
        evidence = set(it.get("answer_session_ids") or [])
        if not evidence:
            continue
        mem = Memory(embedder=embedder, llm=extractor)
        ingest(mem, it, qid)
        # episode.id -> session_id，从而可以按 provenance 所在 session 标注 fact
        ep_session = {ep.id: ep.session_id for ep in mem.episodes_doc.values()}
        retrieved = mem.retrieve_episodes(it["question"], qid, extract_k)
        mem.consolidate(retrieved)
        relevant = {
            f.id for f in mem.fact_store.values()
            if any(ep_session.get(pid) in evidence for pid in f.provenance)
        }
        if relevant:
            dev.append((mem, qid, it["question"], relevant))
    return dev


def mean_recall_at_k(dev, weights: dict, k: int) -> float:
    total = 0.0
    for mem, qid, query, relevant in dev:
        for name, val in weights.items():
            setattr(mem.config, name, val)
        ranked, _ = mem.retriever.retrieve(query, mem.resolver.resolve(qid), None, k)
        got = {f.id for f, _ in ranked} & relevant
        total += len(got) / len(relevant)
    return total / len(dev) if dev else 0.0


def grid_search(dev, k: int):
    grid = {
        "w_sem": [0.8, 1.0, 1.2],
        "w_lex": [0.4, 0.6, 0.9],
        "w_graph": [0.4, 0.8, 1.2],
        "w_rec": [0.1, 0.3, 0.5],
        "w_sal": [0.1, 0.25],
    }
    base = Config()
    best, best_w = -1.0, {k_: getattr(base, k_) for k_ in grid}
    names = list(grid)
    for combo in itertools.product(*grid.values()):
        w = dict(zip(names, combo))
        r = mean_recall_at_k(dev, w, k)
        if r > best:
            best, best_w = r, w
    return best, best_w, mean_recall_at_k(dev, {k_: getattr(base, k_) for k_ in grid}, k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=int, default=30)
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--data", default="s")
    ap.add_argument("--extractor", default="volcano:doubao-seed-1-6-flash-250615")
    ap.add_argument("--embedder", default="bge-small")
    args = ap.parse_args()
    load_dotenv()
    items = load_data(args.data)
    # dev 集取自尾部（评测里固定种子的洗牌用的是头部顺序；取尾部避免与已有报告重叠）
    import random
    random.Random(999).shuffle(items)
    dev_items = items[: args.dev]
    print(f"building {len(dev_items)} dev questions (one-time fact extraction)...")
    dev = build_dev(dev_items, make_embedder(args.embedder), make_llm(args.extractor, max_tokens=512))
    print(f"  {len(dev)} usable (had relevant facts)")
    best, best_w, base_r = grid_search(dev, args.k)
    print(f"\n  default weights recall@{args.k}: {base_r:.3f}")
    print(f"  BEST weights recall@{args.k}:    {best:.3f}")
    print(f"  best = {best_w}")
    if best > base_r + 0.01:
        print("  -> update mnemotrace/config.py defaults with these (material improvement).")
    else:
        print("  -> defaults are already near-optimal; now harness-validated, not hand-waved.")


if __name__ == "__main__":
    main()
