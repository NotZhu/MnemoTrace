"""外科手术式诊断：对指定的 qids，同时构建 MnemoTrace 上下文（facts+chunks）和 rag 上下文
（仅 chunks），与金标准答案并排打印。目的：看清 facts 层究竟为什么把 knowledge-update /
preference 的答案相对朴素 RAG 翻错（scale50：engram 72 vs rag 74，差距全在 knowledge-update
75 vs 88）。按 M1 infra 笔记离线运行。

    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python eval/diag_facts.py 945e3d21 1c0ddc50 505af2f5
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Memory  # noqa: E402
from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm  # noqa: E402
from eval.longmemeval import ingest, load_data, sessions_of  # noqa: E402


def main():
    qids = set(sys.argv[1:]) or {"945e3d21"}
    load_dotenv()
    emb = make_embedder("bge-small")
    extractor = make_llm("deepseek")
    items = {it["question_id"]: it for it in load_data("s")}

    for qid in qids:
        item = items.get(qid)
        if item is None:
            print(f"!! {qid} not in _s"); continue
        q = item["question"]
        print("=" * 100)
        print(f"QID {qid}  [{item.get('question_type')}]  date={item.get('question_date','')}")
        print(f"Q: {q}")
        print(f"GOLD: {item.get('answer')}")
        n_sessions = len(list(sessions_of(item)))
        print(f"(haystack: {n_sessions} sessions)")

        # MNEMOTRACE：facts + chunks（镜像 MnemoTraceSystem）
        mem = Memory(embedder=emb, llm=extractor)
        ingest(mem, item, qid)
        mem.engine.consolidate(mem.retrieve_episodes(q, qid, 8))
        eng_ctx = mem.context_for(q, user_id=qid, top_k=10, k_chunks=5)
        facts_block = eng_ctx.split("RELEVANT CONVERSATIONS")[0]
        print("\n----- MNEMOTRACE facts block (the only thing rag DOESN'T see) -----")
        print(facts_block.strip()[:2500])

        # facts 是否让答案中的实体浮出水面？按主体列出 live 与失效化的 fact。
        user = qid
        live = [f for f in mem.fact_store.values() if f.user_id == user and f.is_live()]
        dead = [f for f in mem.fact_store.values() if f.user_id == user and not f.is_live()]
        print(f"\n  fact_store: {len(live)} live, {len(dead)} invalidated (supersede chain)")
        if dead:
            print("  INVALIDATED (should be old values):")
            for f in dead[:8]:
                print(f"    - {f.text}  [valid_at={f.valid_at}]")
    print("=" * 100)


if __name__ == "__main__":
    main()
