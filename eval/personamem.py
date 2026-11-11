"""PersonaMem-v2 运行器（AGENTS.md 「评测、公开内容与贡献规则」 目标 benchmark 第 4 项）。

PersonaMem 是四选一的 PERSONALIZATION 任务：给定一段隐含暴露用户偏好的长聊天历史（约 32k token），
从 4 个候选回复中选出对 THIS 用户最个性化的那一个。三个干扰项指向看似合理但错误的偏好。打分是
精确选项命中率 —— 不用 LLM judge —— 因而能单独衡量 memory 层是否为查询浮出了正确的偏好。

它为何补充 LongMemEval：它考验隐式偏好抽取 + L3 persona/profile 层，且包含 `updated`（历史中途偏好
变更 -> 必须以最新为准，正是我们双时间轴/冲突处理的强项）和 `who`（自己 vs 他人）。我们让
engram_lean（从精简检索切片作答）对阵 full-context 基线（整段历史进 prompt），用同一个 answerer
—— 诚实的对比。

    python eval/personamem.py --n-personas 20 --per-persona 5 --answerer volcano:doubao-seed-1-6-flash-250615 \
        --systems engram_lean,full_context --workers 6 --out .artifacts/eval/personamem_smoke.jsonl
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import random
import re
import sys
import threading
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")  # litellm/pydantic 对 doubao 回复会发出无害的序列化警告

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import MemoryEngine  # noqa: E402
from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm  # noqa: E402

HF = "https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2/resolve/main"
PM_DIR = "data/personamem"
CH_DIR = f"{PM_DIR}/ch32k"
BASE_T = 1_700_000_000.0  # 用于给 episode 排序的合成纪元（PersonaMem 没有 per-turn 日期）

ANSWER_SYSTEM = (
    "You select the MOST personalized response for a specific user, using ONLY what you know about them "
    "below. Pick the single option that best fits THIS user's stated preferences, habits, situation, and "
    "constraints. If a preference was updated over time, honor the most recent one. Reply with ONLY the "
    "letter (A, B, C, or D) — nothing else."
)


def _certifi():
    import certifi
    return certifi.where()


def _download(path: str, dest: str) -> None:
    import requests
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = requests.get(f"{HF}/{path}", timeout=120, verify=_certifi())
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)


def history_for(link: str) -> list[dict]:
    """下载并缓存某个 persona 的 32k 聊天历史（{role, content} 消息列表）。"""
    dest = f"{CH_DIR}/{os.path.basename(link)}"
    if not os.path.exists(dest):
        _download(link, dest)
    with open(dest) as f:
        return json.load(f)["chat_history"]


def load_items(n_personas: int, per_persona: int, seed: int = 0) -> list[dict]:
    """从 benchmark.csv 抽取均衡子集：n_personas 个 persona，每个至多 per_persona 条查询。"""
    import pandas as pd
    df = pd.read_csv(f"{PM_DIR}/benchmark.csv")
    rng = random.Random(seed)
    pids = sorted(df.persona_id.unique())
    rng.shuffle(pids)
    items: list[dict] = []
    for pid in pids[:n_personas]:
        rows = df[df.persona_id == pid]
        idx = list(rows.index)
        rng.shuffle(idx)
        for i in idx[:per_persona]:
            r = df.loc[i]
            uq = r.user_query
            try:
                uq = ast.literal_eval(uq).get("content", uq) if str(uq).strip().startswith("{") else uq
            except Exception:
                pass
            try:
                incorrect = ast.literal_eval(r.incorrect_answers)
            except Exception:
                continue
            if not isinstance(incorrect, list) or len(incorrect) < 3:
                continue
            items.append({
                "qid": f"p{pid}_{i}",
                "persona_id": int(pid),
                "link": r.chat_history_32k_link,
                "query": str(uq),
                "correct": str(r.correct_answer),
                "incorrect": [str(x) for x in incorrect[:3]],
                "pref_type": str(r.pref_type),
                "updated": bool(r.updated),
                "who": str(r.who),
            })
    return items


def ingest_history(mem: MemoryEngine, messages: list[dict], pid: str, chunk: int = 6) -> None:
    """把对话按顺序摄取为 episode（system persona = episode 0；之后每段约 chunk 条消息）。
    event_time 递增保持顺序，让 UPDATED 偏好以最近一次出现为准（双时间轴）。"""
    eps: list[tuple[str, float]] = []
    if messages and messages[0].get("role") == "system":
        eps.append((f"USER PERSONA:\n{messages[0]['content']}", BASE_T))
        body = messages[1:]
    else:
        body = messages
    for i in range(0, len(body), chunk):
        seg = body[i:i + chunk]
        text = "\n".join(f"{m.get('role','user')}: {m.get('content','')}" for m in seg if m.get("content"))
        if text.strip():
            eps.append((text, BASE_T + (i + 1) * 3600.0))
    for text, et in eps:
        mem.add(text, user_id=pid, session_id=f"seg{int(et)}", role="session", event_time=et)


def full_history_text(messages: list[dict], budget: int = 120_000) -> str:
    out = []
    for m in messages:
        c = m.get("content", "")
        if c:
            out.append(f"[{m.get('role','user')}] {c}")
    return "\n".join(out)[:budget]


def ask_mc(llm, context: str, query: str, options: list[str]) -> int:
    """把 4 个选项以 A-D 呈现，返回所选下标（解析失败返回 -1）。"""
    letters = ["A", "B", "C", "D"]
    opt_block = "\n".join(f"{letters[i]}) {o}" for i, o in enumerate(options))
    prompt = (f"What you know about the user:\n{context}\n\n"
              f"The user asks: \"{query}\"\n\n"
              f"Which response is the MOST personalized for THIS user?\n{opt_block}\n\nAnswer (one letter):")
    raw = llm.complete(prompt, system=ANSWER_SYSTEM)
    m = re.search(r"[ABCD]", raw.upper())
    return letters.index(m.group(0)) if m else -1


# ---- 按 persona 缓存 MnemoTrace memory（摄取一次，回答其全部查询）----
_MEM_LOCK = threading.Lock()
_MEM_CACHE: dict[str, MemoryEngine] = {}


def mnemotrace_mem(pid: str, messages: list[dict], embedder, extractor) -> MemoryEngine:
    with _MEM_LOCK:
        if pid in _MEM_CACHE:
            return _MEM_CACHE[pid]
    mem = MemoryEngine(embedder=embedder, llm=extractor)
    ingest_history(mem, messages, pid)
    mem.consolidate_full()
    with _MEM_LOCK:
        _MEM_CACHE[pid] = mem
    return mem


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-personas", type=int, default=20)
    ap.add_argument("--per-persona", type=int, default=5)
    ap.add_argument("--systems", default="engram_lean,full_context")
    ap.add_argument("--answerer", default="volcano:doubao-seed-1-6-flash-250615")
    ap.add_argument("--extractor", default="volcano:doubao-seed-1-6-flash-250615")
    ap.add_argument("--embedder", default="bge-small")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--topk", type=int, default=12)
    ap.add_argument("--n-digests", type=int, default=15, dest="n_digests")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    load_dotenv()
    systems = args.systems.split(",")
    embedder = make_embedder(args.embedder)
    answerer = make_llm(args.answerer, max_tokens=8, num_retries=3, timeout=60)
    extractor = make_llm(args.extractor, max_tokens=512, num_retries=3, timeout=60)
    rig = {"topk": args.topk, "n_digests": args.n_digests}

    items = load_items(args.n_personas, args.per_persona, args.seed)
    print(f"PersonaMem-v2 | {len(items)} questions / {args.n_personas} personas | systems={systems}", flush=True)
    print(f"  answerer={args.answerer}  extractor={args.extractor}  embedder={args.embedder}  workers={args.workers}", flush=True)

    done = set()
    if args.out and os.path.exists(args.out):
        done = {json.loads(l)["qid"] for l in open(args.out)}
    todo = [it for it in items if it["qid"] not in done]
    out_fh = open(args.out, "a") if args.out else None
    out_lock = threading.Lock()
    hist_cache: dict[str, list] = {}

    def one(it):
        try:
            link = it["link"]
            with _MEM_LOCK:
                msgs = hist_cache.get(link)
            if msgs is None:
                msgs = history_for(link)
                with _MEM_LOCK:
                    hist_cache[link] = msgs
            pid = str(it["persona_id"])
            # 每个 qid 确定性地打乱选项
            rng = random.Random(it["qid"])
            opts = [("c", it["correct"])] + [("x", x) for x in it["incorrect"]]
            rng.shuffle(opts)
            correct_idx = next(i for i, (tag, _) in enumerate(opts) if tag == "c")
            option_texts = [o for _, o in opts]
            res = {}
            for sysname in systems:
                t0 = time.perf_counter()
                if sysname == "full_context":
                    ctx = full_history_text(msgs)
                else:
                    mem = mnemotrace_mem(pid, msgs, embedder, extractor)
                    ctx = mem.lean_context(it["query"], user_id=pid, n_facts=rig["topk"],
                                           n_digests=rig["n_digests"])
                pick = ask_mc(answerer, ctx, it["query"], option_texts)
                res[sysname] = {"ok": pick == correct_idx, "pick": pick, "tok": len(ctx.split()),
                                "lat": (time.perf_counter() - t0) * 1000.0}
            return {"qid": it["qid"], "pref_type": it["pref_type"], "updated": it["updated"],
                    "who": it["who"], "sys": res}
        except Exception as e:  # noqa: BLE001
            return {"qid": it["qid"], "err": f"{type(e).__name__}: {str(e)[:100]}"}

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(one, todo)):
            results.append(r)
            if out_fh and "err" not in r:
                with out_lock:
                    out_fh.write(json.dumps(r) + "\n"); out_fh.flush()
            if (i + 1) % 10 == 0 or i + 1 == len(todo):
                acc = {s: f"{100*sum(1 for x in results if 'sys' in x and x['sys'].get(s,{}).get('ok'))/max(1,sum(1 for x in results if 'sys' in x)):.0f}%" for s in systems}
                print(f"  [{i+1}/{len(todo)}] {acc}", flush=True)
    if out_fh:
        out_fh.close()

    # 合并 --out 里已有的历史结果，用于最终报告
    allr = results + [json.loads(l) for l in open(args.out)] if (args.out and os.path.exists(args.out)) else results
    seen = set(); merged = []
    for r in allr:
        if r.get("qid") not in seen and "sys" in r:
            seen.add(r["qid"]); merged.append(r)
    print(f"\n===== PersonaMem-v2 ({len(merged)} scored) =====", flush=True)
    for s in systems:
        ok = sum(1 for r in merged if r["sys"].get(s, {}).get("ok"))
        tok = [r["sys"][s]["tok"] for r in merged if s in r["sys"]]
        print(f"  {s:14} acc={100*ok/max(1,len(merged)):5.1f}%  avg_tokens={sum(tok)//max(1,len(tok))}", flush=True)
    # engram_lean 的分类别细分（最有看头的切分）
    if "engram_lean" in systems:
        print("  -- engram_lean by pref_type / updated --", flush=True)
        by = defaultdict(lambda: [0, 0])
        for r in merged:
            for key in (r["pref_type"], "updated" if r["updated"] else "static"):
                by[key][0] += r["sys"].get("engram_lean", {}).get("ok", False); by[key][1] += 1
        for k, (o, n) in sorted(by.items()):
            print(f"     {k:28} {o:3}/{n:3} = {100*o/n:5.1f}%", flush=True)


if __name__ == "__main__":
    run()
