"""忆痕 快速上手——完整管线，零配置，无需 API key。

本模块同时支撑源码检出的 ``python examples/quickstart.py`` 和安装后的
``mnemotrace-quickstart`` 控制台脚本。
"""
from __future__ import annotations

from .memory import Memory
from .util import DAY


BASE = 1_700_000_000.0  # 固定纪元，保证 demo 可复现


def main() -> None:
    mem = Memory()

    history = [
        ("My name is Wei and I work at Tencent.", 0),
        ("I live in Shenzhen.", 1),
        ("My favorite programming language is Python.", 2),
        ("Actually I just switched jobs — I now work at Moonshot AI.", 30),
        ("My colleague Lin works at Moonshot AI too.", 31),
    ]

    print("== Ingest (System-1, fast write path) ==")
    for text, day in history:
        mem.add(text, user_id="wei", event_time=BASE + day * DAY)
        print(f"   day {day:>2}: {text}")

    print("\n== Consolidate (System-2: extract facts, build graph, resolve conflicts) ==")
    print("  ", mem.consolidate())

    print("\n== Current profile (L3 identity) ==")
    print("  ", mem.profile("wei"))

    print("\n== Queries ==")
    for q in [
        "Where does Wei work?",  # 单跳，必须反映换工作
        "What is my favorite programming language?",  # 第一人称属性
        "Which city does Wei live in?",
        "Where does Wei's colleague work?",  # 多跳：Wei -> Lin -> Moonshot
        "What is the capital of France?",  # 记忆中没有 -> 拒答
    ]:
        r = mem.search(q, user_id="wei")
        print(f"   [{r.via:9}] {q}\n              -> {r.answer()!r}")

    print("\n== Knowledge update: history of Wei's employer (nothing is deleted) ==")
    for f in mem.history("Wei", "works_at", user_id="wei"):
        state = "current" if f.is_live() else "superseded"
        print(f"   {f.object:12} [{state}]  supersedes={f.supersedes}")

    print("\n== Bi-temporal as-of query: what did we believe on day 10 (before the switch)? ==")
    r = mem.as_of("Where does Wei work?", BASE + 10 * DAY, user_id="wei")
    print(f"   day 10 -> {r.answer()!r}   (today -> {mem.search('Where does Wei work?', user_id='wei').answer()!r})")

if __name__ == "__main__":
    main()
