"""忆痕 快速上手——使用已配置的 LLM 运行完整管线。

本模块同时支撑源码检出的 ``python examples/quickstart.py`` 和安装后的
``mnemotrace-quickstart`` 控制台脚本。
"""
from __future__ import annotations

from .memory import Memory
from .util import DAY


BASE = 1_700_000_000.0  # 固定纪元，保证 demo 可复现


def main() -> None:
    import os
    from .llm.providers import load_dotenv, make_llm
    from .extraction import LLMNotConfiguredError

    load_dotenv()
    model = os.environ.get("MNEMOTRACE_LLM", "").strip()
    if not model:
        raise SystemExit(str(LLMNotConfiguredError()) + " Install mnemotrace-memory[llm].")
    try:
        mem = Memory(llm=make_llm(model))
    except ImportError:
        raise SystemExit('Install mnemotrace-memory[llm] to run the quickstart.') from None

    history = [
        ("My name is Wei and I work at Tencent.", 0),
        ("I live in Shenzhen.", 1),
        ("My favorite programming language is Python.", 2),
        ("Actually I just switched jobs — I now work at Moonshot AI.", 30),
        ("My colleague Lin works at Moonshot AI too.", 31),
    ]

    print("== 写入原始事件（System-1，快速写入） ==")
    for text, day in history:
        mem.add(text, user_id="wei", event_time=BASE + day * DAY)
        print(f"   第 {day:>2} 天：{text}")

    print("\n== 记忆整合（System-2：提取事实、构建图关系、处理冲突） ==")
    print("  ", mem.consolidate())

    print("\n== 当前用户画像（L3 身份信息） ==")
    print("  ", mem.profile("wei"))

    print("\n== 查询记忆 ==")
    for q in [
        "Where does Wei work?",  # 单跳，必须反映换工作
        "What is my favorite programming language?",  # 第一人称属性
        "Which city does Wei live in?",
        "Where does Wei's colleague work?",  # 多跳：Wei -> Lin -> Moonshot
        "What is the capital of France?",  # 记忆中没有 -> 拒答
    ]:
        r = mem.search(q, user_id="wei")
        print(f"   [{r.via:9}] {q}\n              -> {r.answer()!r}")

    print("\n== 记忆更新：Wei 的工作单位历史 ==")
    for f in mem.history("Wei", "works_at", user_id="wei"):
        state = "当前" if f.is_live() else "已被取代"
        print(f"   {f.object:12} [{state}]  取代的事实 ID={f.supersedes}")

    print("\n== 时间查询：第 10 天（换工作前）记录的工作单位 ==")
    r = mem.as_of("Where does Wei work?", BASE + 10 * DAY, user_id="wei")
    print(f"   第 10 天 -> {r.answer()!r}   （当前 -> {mem.search('Where does Wei work?', user_id='wei').answer()!r}）")

if __name__ == "__main__":
    main()
