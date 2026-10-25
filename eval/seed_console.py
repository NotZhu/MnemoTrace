"""用 LongMemEval_S 给 MnemoTrace 控制台做种子，让部署好的服务有丰富、真实的记忆可浏览、可测试
——每个问题一个独立用户命名空间（namespace = 问题序号）。

为什么本地构建再上传：先选择与服务端相同的 embedder，在本地构建每用户的存储
（向量兼容）。生成到独立的 --out 目录，停服后按清单将完整的存储目录拷进
MNEMOTRACE_DATA_DIR，保留目录名且拒绝覆盖；用相同 embedder 启动服务。输出冲突会报错，
失败后换新的 --out 目录重新生成，不自动清理或导入旧输出。完整步骤见 eval/README.md。

混合抽取（成本与质量的折中）：大头用免费的离线规则抽取器；有代表性的子集——每个类别的前
`--featured-per-cat` 个问题——用配置好的 LLM 抽取高质量 fact。其余一切（episode、L2 摘要、
时间线、图、检索）无论哪种方式都会填充。

从仓库根目录运行（以便读到带 provider key 的 .env）：
  python eval/seed_console.py \
      --data ~/.cache/huggingface/hub/datasets--xiaowu0162--longmemeval/snapshots/*/longmemeval_s \
      --out /tmp/console_stores --featured-per-cat 3 --llm volcano:doubao-seed-1-6-flash-250615
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mnemotrace import Memory
from mnemotrace._namespace_paths import namespace_path
from mnemotrace.util import DAY, fmt_date

BASE = 1_700_000_000.0


# --- LongMemEval ingest（从 eval/longmemeval.py 内联，避免引入 LLM 评测栈） ---
def sessions_of(item: dict):
    sessions = item.get("haystack_sessions", [])
    ids = item.get("haystack_session_ids") or [f"sess_{i}" for i in range(len(sessions))]
    return list(zip(ids, sessions))


def parse_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    tm = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp(), f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def ingest(mem: Memory, item: dict, user_id: str) -> None:
    """每个 session 一个 episode（拼接各 turn），用一次 encode 调用批量嵌入。"""
    dates = item.get("haystack_dates") or []
    texts, meta = [], []
    for idx, (sid, turns) in enumerate(sessions_of(item)):
        parsed = parse_date(dates[idx] if idx < len(dates) else None)
        event_time, dstr = parsed if parsed else (BASE + idx * DAY, None)
        text = "\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}"
                         for t in turns if t.get("content"))
        if text.strip():
            texts.append(text)
            meta.append((sid, event_time, dstr))
    if not texts:
        return
    vecs = mem.embedder.embed_batch(texts)
    for (sid, event_time, dstr), text, vec in zip(meta, texts, vecs):
        ep = mem.add(text, user_id=user_id, session_id=sid, speaker="session",
                     event_time=event_time, embedding=vec)
        ep.metadata["date"] = dstr or fmt_date(event_time)


def pick_featured(items: list[dict], per_cat: int) -> set[int]:
    """每个类别的前 `per_cat` 个问题——覆盖 6 种任务类型的有代表性样本。"""
    seen: dict[str, int] = {}
    featured: set[int] = set()
    for i, it in enumerate(items):
        cat = it.get("question_type", "?")
        if seen.get(cat, 0) < per_cat:
            seen[cat] = seen.get(cat, 0) + 1
            featured.add(i)
    return featured


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="path to longmemeval_s json")
    ap.add_argument(
        "--out",
        default="/tmp/console_stores",
        help="staging root for service-compatible JSONL store directories; existing outputs are rejected",
    )
    ap.add_argument("--embedder", default="bge-small")
    ap.add_argument("--llm", default="volcano:doubao-seed-1-6-flash-250615")
    ap.add_argument("--featured-per-cat", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--device", default=None, help="embedder device: mps | cpu | cuda (default auto)")
    ap.add_argument("--batch", type=int, default=64, help="embedder batch size")
    ap.add_argument("--shard", default=None, help="i/N — only process items where index %% N == i (parallel)")
    args = ap.parse_args()

    from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm

    load_dotenv()
    args.out = os.path.abspath(os.path.expanduser(args.out))

    with open(os.path.expanduser(args.data), encoding="utf-8") as fh:
        items = json.load(fh)
    if args.limit:
        items = items[: args.limit]

    shard_i, shard_n = (None, None)
    if args.shard:
        try:
            shard_i, shard_n = (int(x) for x in args.shard.split("/"))
            if not 0 <= shard_i < shard_n:
                raise ValueError
        except ValueError:
            ap.error("--shard must be i/N with 0 <= i < N")

    selected = [
        (i, item, str(i + 1), namespace_path(args.out, str(i + 1)))
        for i, item in enumerate(items)
        if i >= args.offset and (shard_n is None or i % shard_n == shard_i)
    ]
    mfile = f"manifest.shard{shard_i}.json" if shard_n is not None else "manifest.json"
    manifest_paths = [os.path.join(args.out, mfile)]
    if shard_n is None:
        manifest_paths.append(os.path.join(args.out, "manifest.md"))
    for path in [entry[3] for entry in selected] + manifest_paths:
        if os.path.lexists(path):
            ap.error(f"output already exists: {path}; use a fresh --out directory")
    # Reserve this run's outputs before loading models. Disjoint shards can share a root;
    # a competing run for the same space or manifest must fail instead of overwriting it.
    try:
        os.makedirs(args.out, exist_ok=True)
        for path in manifest_paths:
            with open(path, "x", encoding="utf-8"):
                pass
        for _, _, _, path in selected:
            os.mkdir(path)
    except FileExistsError as exc:
        ap.error(f"output already exists: {exc.filename}; use a fresh --out directory")

    embedder = make_embedder(args.embedder, device=args.device, batch_size=args.batch)
    llm = make_llm(args.llm) if args.llm else None
    featured = pick_featured(items, args.featured_per_cat) if llm else set()
    print(f"{len(items)} questions | featured (LLM): {len(featured)} | offline: {len(items) - len(featured)}")

    manifest = []
    t0 = time.time()
    for i, item, namespace, path in selected:
        is_feat = i in featured
        mem = Memory(embedder=embedder, llm=(llm if is_feat else None))
        ingest(mem, item, user_id=namespace)
        try:
            mem.consolidate()
            mem.summarize_episodes(list(mem.episodes_doc.values()))
        except Exception as exc:  # noqa: BLE001 -- 绝不让一个坏条目中断整个种子流程
            print(f"  [{namespace}] consolidate degraded: {type(exc).__name__}: {exc}")
        n_live = sum(1 for f in mem.fact_store.values() if f.is_live())
        mem.save(path)
        manifest.append({
            "namespace": namespace,
            "directory": os.path.basename(path),
            "category": item.get("question_type", "?"),
            "question": item.get("question", ""),
            "answer": str(item.get("answer", "")),
            "sessions": len(sessions_of(item)),
            "facts_live": n_live,
            "extractor": "llm" if is_feat else "offline",
        })
        if (i + 1) % 10 == 0 or i + 1 == len(items):
            rate = (i + 1 - args.offset) / max(1e-6, time.time() - t0)
            print(f"  {i + 1}/{len(items)}  ({rate:.1f}/s)  last={namespace} facts={n_live} "
                  f"{'★llm' if is_feat else ''}")

    # 分片时每个 worker 只写自己的清单，不生成合并清单或 Markdown。
    with open(os.path.join(args.out, mfile), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    if shard_n is not None:
        print(f"shard {shard_i}/{shard_n} done in {time.time() - t0:.0f}s -> {mfile} ({len(manifest)} users)")
        return
    # 人类可读的表格
    lines = ["# MnemoTrace console seed — LongMemEval_S\n",
             f"{len(manifest)} namespaces. ★ = LLM-extracted.\n",
             "开放模式使用 namespace 登录；受保护模式使用管理员映射到该 namespace 的 API Key。\n",
             "停服后将清单中的完整 directory 复制到 MNEMOTRACE_DATA_DIR，保留目录名且拒绝覆盖；"
             "使用与生成时相同的 embedder 启动服务。完整步骤见 eval/README.md。\n",
             "| namespace | directory | cat | facts | question | answer |",
             "|--:|---|---|--:|---|---|"]
    for m in manifest:
        star = " ★" if m["extractor"] == "llm" else ""
        q = m["question"].replace("|", "\\|")[:90]
        a = m["answer"].replace("|", "\\|").replace("\n", " ")[:60]
        lines.append(f"| {m['namespace']}{star} | {m['directory']} | {m['category']} | "
                     f"{m['facts_live']} | {q} | {a} |")
    with open(os.path.join(args.out, "manifest.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"done in {time.time() - t0:.0f}s -> {args.out} (manifest.json + manifest.md)")


if __name__ == "__main__":
    main()
