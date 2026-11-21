"""用实际推理端点和独立 PostgreSQL 验收记忆写入与召回闭环。"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool
from recall_quality import _quality_database

from mnemotrace_next import (
    GraphRecallOptions,
    Memory,
    MessageInput,
    RecallRelation,
    RememberResult,
)
from mnemotrace_next.adapters.inference import InferenceRequestError, open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import DigestId, FactId, MessageId, SessionId, SpaceId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue
from mnemotrace_next.ports.inference import (
    CompletionRefused,
    CompletionTruncated,
    InferenceError,
    InvalidStructuredOutput,
)

_ROUNDS = 3
_MIN_PASSES = 2
_TOP_K = 5
_FIRST_AT = datetime(2026, 1, 10, 9, tzinfo=UTC).timestamp()
_CHANGE_AT = datetime(2026, 1, 20, 9, tzinfo=UTC).timestamp()
_NOW = datetime(2026, 2, 1, tzinfo=UTC).timestamp()
_OWNER = "memory-closure"


class _FixedClock:
    def now(self) -> float:
        return _NOW


class _Ids:
    def new_space_id(self) -> SpaceId:
        return SpaceId(uuid4().hex)

    def new_session_id(self) -> SessionId:
        return SessionId(uuid4().hex)

    def new_message_id(self) -> MessageId:
        return MessageId(uuid4().hex)

    def new_fact_id(self) -> FactId:
        return FactId(uuid4().hex)

    def new_digest_id(self) -> DigestId:
        return DigestId(uuid4().hex)


@dataclass
class _Trace:
    name: str
    space_id: SpaceId
    writes: list[dict[str, object]] = field(default_factory=list)
    stored_facts: list[Fact] = field(default_factory=list)
    recalls: list[dict[str, object]] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    error: dict[str, object] | None = None

    @property
    def passed(self) -> bool:
        return not (self.misses or self.violations or self.error)


@dataclass
class _Case:
    memory: Memory
    storage: PostgresStorage
    trace: _Trace

    async def write(self, content: str, at: float) -> RememberResult:
        session = await self.memory.create_session()
        message = MessageInput("user", content, at)
        entry: dict[str, object] = {"session_id": session, "input": message}
        self.trace.writes.append(entry)
        result = await self.memory.remember((message,), session_id=session)
        entry["result"] = result
        async with self.storage.session(self.trace.space_id) as reader:
            stored = await reader.get_messages(result.message_ids)
        if len(stored) != 1 or any(
            (row.session_id, row.content, row.event_time) != (session, content, at)
            for row in stored.values()
        ):
            self.trace.violations.append("已确认的原文与本次写入不符")
        return result

    async def read_writes(self, *writes: RememberResult) -> None:
        latest = {
            change.after.id: change.after
            for result in writes
            for change in result.fact_changes
        }
        async with self.storage.session(self.trace.space_id) as reader:
            stored = await reader.get_facts(tuple(latest))
        self.trace.stored_facts.extend(stored.values())
        if stored != latest:
            self.trace.violations.append("实际存储事实与最后一次已确认的变更不符")

    def written_fact(
        self, result: RememberResult, name: str, matches: Callable[[Fact], bool]
    ) -> Fact | None:
        facts = [
            change.after
            for change in result.fact_changes
            if change.before is None and matches(change.after)
        ]
        if len(facts) != 1:
            self.trace.misses.append(f"{name}抽取结果应有一条，实际 {len(facts)} 条")
            return None
        (fact,) = facts
        if fact.provenance != result.message_ids:
            self.trace.violations.append(f"{name}的来源与发言消息不符")
        return fact

    async def recall(
        self,
        query: str,
        *,
        as_of: float | None = None,
        graph: GraphRecallOptions | None = None,
    ) -> set[FactId]:
        entry: dict[str, object] = {
            "query": query,
            "top_k": _TOP_K,
            "as_of": as_of,
            "graph": graph,
        }
        self.trace.recalls.append(entry)
        result = await self.memory.recall(query, top_k=_TOP_K, as_of=as_of, graph=graph)
        entry["result"] = result
        if result.degraded_reason:
            self.trace.misses.append(f"召回降级 {result.degraded_reason}")
        ids = tuple(item.fact_id for item in result.evidence)
        async with self.storage.session(self.trace.space_id) as reader:
            stored = await reader.get_facts(ids)
        at = _NOW if as_of is None else as_of
        if len(ids) > _TOP_K or len(set(ids)) != len(ids):
            self.trace.violations.append("召回返回了重复事实或超出 top_k")
        for item in result.evidence:
            fact = stored.get(item.fact_id)
            if fact is None:
                self.trace.violations.append(
                    f"召回返回了当前空间不可读取的事实 {item.fact_id.value}"
                )
                continue
            if (
                (fact.valid_at is None and as_of is not None)
                or (fact.valid_at is not None and at < fact.valid_at)
                or (fact.invalid_at is not None and at >= fact.invalid_at)
            ):
                self.trace.violations.append(
                    f"召回返回了查询时点无效的事实 {item.fact_id.value}"
                )
            relation = (
                RecallRelation(
                    fact.subject.entity_id, fact.predicate, fact.object.entity_id
                )
                if isinstance(fact.object, EntityRef)
                else None
            )
            if (
                item.text,
                item.valid_at,
                item.invalid_at,
                item.source_message_ids,
                item.relation,
            ) != (fact.text, fact.valid_at, fact.invalid_at, fact.provenance, relation):
                self.trace.violations.append(
                    f"召回证据与实际存储事实不符 {item.fact_id.value}"
                )
        return set(ids)


async def _state_change(case: _Case) -> None:
    first = await case.write("星河项目的默认语言是中文。", _FIRST_AT)
    second = await case.write(
        "从 2026-01-20 09:00 UTC 起，星河项目的默认语言改为英文。", _CHANGE_AT
    )
    await case.read_writes(first, second)
    scope = case.trace.space_id
    project = Entity(scope, "星河项目").id

    def is_language(fact: Fact, names: tuple[str, ...]) -> bool:
        targets = {EntityRef(Entity(scope, name).id) for name in names}
        return fact.subject.entity_id == project and (
            fact.object.value in names
            if isinstance(fact.object, LiteralValue)
            else fact.object in targets
        )

    old = case.written_fact(
        first, "旧语言", lambda fact: is_language(fact, ("中文", "简体中文", "汉语"))
    )
    new = case.written_fact(
        second, "新语言", lambda fact: is_language(fact, ("英文", "英语"))
    )
    query = "星河项目的默认语言是什么？"
    for label, at, wanted, excluded in (
        ("变更前", _CHANGE_AT - 1, old, new),
        ("生效时刻", _CHANGE_AT, new, old),
        ("当前", None, new, old),
    ):
        found = await case.recall(query, as_of=at)
        if (wanted and wanted.id not in found) or (excluded and excluded.id in found):
            case.trace.misses.append(f"{label}的语言状态未正确召回")
    if new and new.valid_at != _CHANGE_AT:
        case.trace.misses.append("新语言未抽取到明确的生效时刻")
    if old and new:
        stored = {fact.id: fact for fact in case.trace.stored_facts}
        if old.id in stored and stored[old.id].invalid_at != _CHANGE_AT:
            case.trace.misses.append("旧语言未在新语言的生效时刻截止")


async def _two_hops(case: _Case) -> None:
    query = "QV-1732"
    first = await case.write("QV-1732 由林北维护。", _FIRST_AT)
    second = await case.write("林北隶属潮汐运维组。", _CHANGE_AT)
    await case.read_writes(first, second)

    def edge(fact: Fact, source: str, target: str) -> bool:
        scope = case.trace.space_id
        return fact.subject.entity_id == Entity(
            scope, source
        ).id and fact.object == EntityRef(Entity(scope, target).id)

    bridge = case.written_fact(first, "起始边", lambda fact: edge(fact, query, "林北"))
    target = case.written_fact(
        second, "目标边", lambda fact: edge(fact, "林北", "潮汐运维组")
    )
    direct = await case.recall(query)
    if bridge is None or target is None:
        return
    if bridge.id not in direct:
        case.trace.misses.append("普通召回未找到起始边")
    origin = (bridge.subject.entity_id,)
    once = await case.recall(
        query, graph=GraphRecallOptions(origin_entity_ids=origin, max_hops=1)
    )
    twice = await case.recall(
        query, graph=GraphRecallOptions(origin_entity_ids=origin, max_hops=2)
    )
    if target.id in direct or target.id in once or target.id not in twice:
        case.trace.misses.append("目标边应仅在二跳结果中出现")


def _repository() -> dict[str, object]:
    script = Path(__file__).resolve()
    root = script.parents[2]
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root).strip()
    )
    return {
        "commit": commit,
        "dirty": dirty,
        "script_sha256": sha256(script.read_bytes()).hexdigest(),
    }


async def _run(admin_dsn: str, report: dict[str, object]) -> bool:
    async with open_inference(os.environ, llm=True, embed=True) as inference:
        assert inference.llm is not None and inference.embedder is not None
        report["embedding_space"] = inference.embedder.embedding_space
        report["embedding_dim"] = inference.embedder.dim
        with _quality_database(admin_dsn) as dsn:
            with psycopg.connect(dsn) as conn:
                versions = conn.execute(
                    "SELECT current_setting('server_version'), extversion "
                    "FROM pg_extension WHERE extname = 'vector'"
                ).fetchone()
                assert versions is not None
                report["postgresql"], report["pgvector"] = versions
            async with AsyncConnectionPool(
                dsn, open=False, min_size=0, max_size=2
            ) as pool:
                storage = PostgresStorage(pool, owner_id=_OWNER)
                rounds: list[dict[str, object]] = []
                report["rounds"] = rounds
                pass_count = 0
                violated = False
                for number in range(1, _ROUNDS + 1):
                    round_traces = []
                    for scenario in (_state_change, _two_hops):
                        ids = _Ids()
                        scope = Space(
                            ids.new_space_id(),
                            _OWNER,
                            scenario.__name__.lstrip("_"),
                            _NOW,
                        )
                        await storage.create_space(scope)
                        trace = _Trace(scope.name, scope.id)
                        memory = Memory(
                            space_id=scope.id,
                            storage=storage,
                            llm=inference.llm,
                            embedder=inference.embedder,
                            clock=_FixedClock(),
                            ids=ids,
                        )
                        try:
                            await scenario(_Case(memory, storage, trace))
                        except (
                            InferenceError,
                            TimeoutError,
                            CompletionRefused,
                            CompletionTruncated,
                            InvalidStructuredOutput,
                        ) as exc:
                            trace.error = {"type": type(exc).__name__}
                            if isinstance(exc, InferenceRequestError):
                                trace.error.update(
                                    kind=exc.kind, status=exc.status_code
                                )
                        round_traces.append(trace)
                    passed = all(trace.passed for trace in round_traces)
                    pass_count += int(passed)
                    violated |= any(trace.violations for trace in round_traces)
                    rounds.append(
                        {"number": number, "passed": passed, "cases": round_traces}
                    )
                    print(f"第 {number} 轮 {'通过' if passed else '失败'}", flush=True)
                accepted = pass_count >= _MIN_PASSES and not violated
                report.update(
                    passed_rounds=pass_count,
                    has_contract_violation=violated,
                    accepted=accepted,
                )
    return accepted


def main(argv: Sequence[str] | None = None) -> int:
    """记录三轮完整结果并以退出码报告验收状态。

    Args:
        argv: 命令行参数，缺省时读取进程参数。

    Returns:
        满足验收门槛返回 0，否则返回 1。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="保存报告的新文件路径")
    args = parser.parse_args(argv)
    admin_dsn = os.environ.get("MNEMOTRACE_NEXT_TEST_POSTGRES_DSN")
    if not admin_dsn:
        parser.error("请设置独立测试实例的 MNEMOTRACE_NEXT_TEST_POSTGRES_DSN")
    if args.out.exists() or not args.out.parent.is_dir():
        parser.error("--out 须使用已存在目录下的新文件路径")

    report: dict[str, object] = {
        "started_at": datetime.now(UTC).isoformat(),
        "repository": _repository(),
        "config": {
            key: os.environ.get(f"MNEMOTRACE_NEXT_{key}")
            for key in (
                "LLM_MODEL",
                "LLM_JSON_MODE",
                "LLM_TEMPERATURE",
                "LLM_DISABLE_THINKING",
                "EMBED_MODEL",
                "EMBED_REVISION",
            )
        },
        "minimum_passed_rounds": _MIN_PASSES,
        "reference_time": _NOW,
        "accepted": False,
    }
    with args.out.open("x", encoding="utf-8") as output:
        try:
            accepted = asyncio.run(
                _run(admin_dsn, report), loop_factory=asyncio.SelectorEventLoop
            )
        except (ValueError, psycopg.Error, TimeoutError) as exc:
            report.update(accepted=False, error={"type": type(exc).__name__})
            print(f"闭环验收中断（{type(exc).__name__}）", file=sys.stderr)
            accepted = False
        finally:
            json.dump(report, output, ensure_ascii=False, indent=2, default=asdict)
            output.write("\n")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
