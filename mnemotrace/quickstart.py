"""忆痕 快速上手——使用已配置的 LLM 运行完整管线。

本模块同时支撑源码检出的 ``python examples/quickstart.py`` 和安装后的
``mnemotrace-quickstart`` 控制台脚本。
"""
from __future__ import annotations

from .config import Config
from .memory import Memory
from .store.contracts import FactQuery, MessageInput
from .store.postgres import Database, DatabaseConfig, Spaces
from .store.postgres.errors import DatabaseError
from .util import DAY


BASE = 1_700_000_000.0  # 固定纪元，保证 demo 可复现


def main() -> None:
    import os
    from .llm.providers import load_dotenv, make_embedder, make_llm
    from .extraction import LLMNotConfiguredError

    load_dotenv()
    model = os.environ.get("MNEMOTRACE_LLM", "").strip()
    if not model:
        raise SystemExit(str(LLMNotConfiguredError()) + " Install mnemotrace-memory[llm].")
    user_id = os.environ.get('MNEMOTRACE_USER_ID')
    space_id = os.environ.get('MNEMOTRACE_SPACE_ID')
    if not user_id or not space_id:
        raise SystemExit('Set MNEMOTRACE_USER_ID and MNEMOTRACE_SPACE_ID to an existing demo space.')
    embedder_name = os.environ.get('MNEMOTRACE_EMBEDDER', 'hashing')
    try:
        llm = make_llm(model)
        embedder = make_embedder(embedder_name)
        with Database(DatabaseConfig.from_env()) as database:
            scope = Spaces(database).authorize(user_id, space_id)
            memory = Memory(database, scope, config=Config.from_env(), llm=llm, embedder=embedder,
                                    embedding_model='hashing-v1' if embedder_name == 'hashing' else embedder_name)
            run_demo(memory)
    except ImportError:
        raise SystemExit('Install mnemotrace-memory[llm] to run the quickstart.') from None
    except DatabaseError as exc:
        raise SystemExit(str(exc)) from None


def run_demo(memory: Memory) -> None:
    history = [
        ("My name is Wei and I work at Tencent.", 0),
        ("I live in Shenzhen.", 1),
        ("My favorite programming language is Python.", 2),
        ("Actually I just switched jobs — I now work at Moonshot AI.", 30),
        ("My colleague Lin works at Moonshot AI too.", 31),
    ]

    print("== 写入原始事件（System-1，快速写入） ==")
    messages = tuple(
        MessageInput(message_id=f'demo:{index}', role='user', content=text,
                     event_time=BASE + day * DAY, mode='historical')
        for index, (text, day) in enumerate(history)
    )
    result = memory.remember_messages('mnemotrace-quickstart', messages, processing='buffered')
    print('   operations:', ', '.join(result['operation_ids']))
    for text, day in history:
        print(f"   第 {day:>2} 天：{text}")

    print("\n== 记忆整合（System-2：提取事实、构建图关系、处理冲突） ==")
    processing = memory.flush_processing('mnemotrace-quickstart')
    if not processing['ok']:
        raise SystemExit('Dialogue processing incomplete; inspect the reported task errors.')

    print("\n== 查询记忆 ==")
    for q in [
        "What is my favorite programming language?",  # 第一人称属性
        "Which city does Wei live in?",
        "Where does Wei's colleague work?",  # 多跳：Wei -> Lin -> Moonshot
        "What is the capital of France?",  # 记忆中没有 -> 拒答
    ]:
        r = memory.search(q)
        print(f"   [{r.via:9}] {q}\n              -> {r.answer()!r}")

    print("\n== 记忆更新：Wei 的工作单位历史 ==")
    for f in memory.history("Wei", "works_at"):
        state = "当前" if f.is_live() else "已被取代"
        print(f"   {f.object:12} [{state}]  取代的事实 ID={f.supersedes}")

    print("\n== 时间查询：第 10 天（换工作前）记录的工作单位 ==")
    question = "Where does Wei work?"
    historical = memory.search(question, facts=FactQuery(
        subject="Wei", predicate="works_at", valid_at=BASE + 10 * DAY))
    current = memory.search(question, facts=FactQuery(subject="Wei", predicate="works_at"))
    print(f"   第 10 天 -> {historical.answer()!r}   （当前 -> {current.answer()!r}）")

if __name__ == "__main__":
    main()
