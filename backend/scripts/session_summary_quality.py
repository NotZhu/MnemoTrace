"""用合成会话对照滚动摘要开关两侧的写入抽取。"""

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter

import psycopg
from psycopg_pool import AsyncConnectionPool
from recall_quality import _quality_database

from mnemotrace_next import Memory, MessageInput
from mnemotrace_next.adapters.inference import open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.session_summary_refresh import (
    PostgresSessionSummaryRefresh,
)
from mnemotrace_next.adapters.runtime import UuidGenerator
from mnemotrace_next.application.session_summary_refresh import SessionSummaryRunner
from mnemotrace_next.domain.enums import Role
from mnemotrace_next.domain.ids import MessageId
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.space import Space
from mnemotrace_next.ports.inference import (
    LLM,
    Completion,
    CompletionRefused,
    CompletionTruncated,
    Embedder,
    InferenceError,
    InvalidStructuredOutput,
)
from mnemotrace_next.worker import _is_transient

_AT = 1_769_904_000.0
_OWNER = "session-summary-quality"
_MODEL_ERRORS = (
    InferenceError,
    CompletionRefused,
    CompletionTruncated,
    InvalidStructuredOutput,
    TimeoutError,
)


@dataclass(frozen=True)
class _Case:
    name: str
    earlier: tuple[str, ...]
    latest: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()
    improvement_case: bool = False


_CASES = (
    _Case(
        "quoted-personal-rule",
        (
            "接下来的这段会话是我的个人工作约定清单。收尾时，我会用引号写一条我自己已经在执行的长期规则。那些引号只是笔记格式。",
        ),
        "「日常导出一律使用 CSV。」",
        ("导出", "CSV"),
        improvement_case=True,
    ),
    _Case(
        "telegraphic-personal-practice",
        (
            "这段对话用于整理我自己的长期办公习惯。我会用省略主语的简短笔记来收尾，笔记都描述我的做法。",
        ),
        "每个工作日收工前写工作日志，坚持了六个月。",
        ("工作日志", "工作日"),
        improvement_case=True,
    ),
    _Case(
        "revoked-decision",
        (
            "我最初决定今后把备份放在北岸仓。",
            "北岸仓的备份决定已经撤销，之前的规则作废。",
        ),
        "以后我所有备份都放在南仓。",
        ("备份", "南仓"),
        ("北岸",),
    ),
    _Case(
        "missing-original-evidence",
        ("我们商定了曙光表格格式，唯一文件后缀是 .aur。",),
        "今后报表就按上次商定的格式导出吧。",
        (),
    ),
    _Case(
        "multiple-compactions",
        (
            "我曾考虑使用 A3 纸打印工作报表，当时没有确认。",
            "我放弃了 A3 纸方案。橙色封面也只是备选方案。",
            "我决定以后报表采用 A5 纸，封面样式暂时待定。",
        ),
        "再次确认，以后我的工作报表统一用 A5 纸打印。",
        ("报表", "A5"),
        ("A3", "橙色"),
    ),
)


class _FixedClock:
    def now(self) -> float:
        return _AT


@dataclass
class _Call:
    fields: object
    input_chars: int
    output: dict[str, object] | None = None
    error: str | None = None


@dataclass
class _Trace:
    enabled: bool
    summaries: list[str] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)
    facts: list[dict[str, object]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    write_ms: float | None = None
    correct: bool = False

    @property
    def valid(self) -> bool:
        return not self.violations and all(call.error is None for call in self.calls)

    def report(self) -> dict[str, object]:
        return {
            **asdict(self),
            "completion_calls": len(self.calls),
            "summary_calls": sum(call.fields == ["summary"] for call in self.calls),
            "input_chars": sum(call.input_chars for call in self.calls),
        }


class _RecordedLLM:
    def __init__(self, llm: LLM, trace: _Trace) -> None:
        self._llm = llm
        self._trace = trace

    async def complete(self, *, system: str, prompt: str) -> Completion:
        return await self._llm.complete(system=system, prompt=prompt)

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        call = _Call(
            schema["required"],
            len(system)
            + len(prompt)
            + len(json.dumps(dict(schema), ensure_ascii=False)),
        )
        self._trace.calls.append(call)
        try:
            result = await self._llm.complete_json(
                system=system, prompt=prompt, schema=schema
            )
        except _MODEL_ERRORS as exc:
            call.error = type(exc).__name__
            raise
        call.output = dict(result)
        return result


def _history(case: _Case) -> tuple[tuple[Role, str], ...]:
    tail = ((Role.ASSISTANT, "继续。"), (Role.USER, "好的。"))
    older: list[tuple[Role, str]] = []
    for item in case.earlier:
        older.extend(((Role.USER, item), (Role.ASSISTANT, "收到。"), *tail * 3))
    return (*older, *tail * 5)


def _correct(case: _Case, trace: _Trace) -> bool:
    if not trace.valid:
        return False
    if not case.required:
        return not trace.facts
    if len(trace.facts) != 1:
        return False
    text = str(trace.facts[0]["text"]).casefold()
    return all(term.casefold() in text for term in case.required) and not any(
        term.casefold() in text for term in case.forbidden
    )


async def _replay(
    storage: PostgresStorage,
    queue: PostgresSessionSummaryRefresh,
    llm: LLM,
    embedder: Embedder,
    case: _Case,
    *,
    enabled: bool,
) -> _Trace:
    trace = _Trace(enabled)
    recorder = _RecordedLLM(llm, trace)
    ids = UuidGenerator()
    scope = Space(
        ids.new_space_id(), _OWNER, case.name, _AT, session_summary_enabled=enabled
    )
    memory = Memory(
        space_id=scope.id,
        storage=storage,
        llm=recorder,
        embedder=embedder,
        clock=_FixedClock(),
        ids=ids,
    )
    runner = SessionSummaryRunner(queue, lambda _: storage, recorder, _is_transient)
    await storage.create_space(scope)
    try:
        session_id = await memory.create_session()
        history = tuple(
            Message(
                MessageId(f"history-{position}"),
                scope.id,
                session_id,
                role,
                text,
                _AT - 100 + position,
                _AT - 100 + position,
            )
            for position, (role, text) in enumerate(_history(case))
        )
        start = 0
        for end in range(18, len(history) + 1, 8):
            async with storage.session(scope.id, write=True) as writer:
                await writer.append_messages(history[start:end])
            start = end
            if enabled:
                before = len(trace.calls)
                while await runner.run_once():
                    pass
                if len(trace.calls) == before:
                    trace.violations.append("该轮未执行摘要补全")
                    continue
                output = trace.calls[-1].output
                generated = output.get("summary") if output is not None else None
                async with storage.session(scope.id) as reader:
                    summary = await reader.get_session_summary(session_id)
                if (
                    summary is None
                    or not isinstance(generated, str)
                    or summary != generated.strip()
                ):
                    trace.violations.append("该轮未发布最新生成的摘要")
                else:
                    trace.summaries.append(summary)

        measured_at = perf_counter()
        try:
            result = await memory.remember(
                (MessageInput("user", case.latest, _AT),), session_id=session_id
            )
        except _MODEL_ERRORS as exc:
            trace.violations.append(type(exc).__name__)
        else:
            allowed = {item.id: item.content for item in history[-10:]}
            allowed.update((item, case.latest) for item in result.message_ids)
            for change in result.fact_changes:
                sources = change.after.provenance
                if not set(sources) <= allowed.keys() or not set(sources).intersection(
                    result.message_ids
                ):
                    trace.violations.append("提交事实包含不合格的原文引用")
                trace.facts.append(
                    {
                        "text": change.after.text,
                        "sources": [
                            {"id": item.value, "content": allowed.get(item)}
                            for item in sources
                        ],
                    }
                )
        trace.write_ms = (perf_counter() - measured_at) * 1000
        trace.correct = _correct(case, trace)
        return trace
    finally:
        await storage.delete_space(scope.id)


async def _run(admin_dsn: str, report: dict[str, object]) -> bool:
    async with open_inference(os.environ, llm=True, embed=True) as models:
        assert models.llm is not None and models.embedder is not None
        with _quality_database(admin_dsn) as dsn:
            async with AsyncConnectionPool(
                dsn, open=False, min_size=0, max_size=2
            ) as pool:
                storage = PostgresStorage(pool, owner_id=_OWNER)
                queue = PostgresSessionSummaryRefresh(pool)
                comparisons: list[dict[str, object]] = []
                report["cases"] = comparisons
                improved = []
                regressions = []
                failed_controls = []
                all_valid = True
                for case in _CASES:
                    off, on = [
                        await _replay(
                            storage,
                            queue,
                            models.llm,
                            models.embedder,
                            case,
                            enabled=value,
                        )
                        for value in (False, True)
                    ]
                    comparisons.append(
                        {
                            "case": case,
                            "history": _history(case),
                            "off": off.report(),
                            "on": on.report(),
                        }
                    )
                    all_valid &= off.valid and on.valid
                    if (
                        case.improvement_case
                        and on.correct
                        and not off.correct
                        and off.valid
                    ):
                        improved.append(case.name)
                    if off.correct and not on.correct:
                        regressions.append(case.name)
                    if not case.improvement_case and not on.correct:
                        failed_controls.append(case.name)
                accepted = (
                    bool(improved)
                    and not (regressions or failed_controls)
                    and all_valid
                )
                report.update(
                    accepted=accepted,
                    improved=improved,
                    regressions=regressions,
                    failed_controls=failed_controls,
                )
    return accepted


def main(argv: Sequence[str] | None = None) -> int:
    """写入逐题对照，退出码反映是否达到验收门槛。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="已存在目录下的一份新报告"
    )
    options = parser.parse_args(argv)
    dsn = os.environ.get("MNEMOTRACE_NEXT_TEST_POSTGRES_DSN")
    if not dsn:
        parser.error("请设置专用测试实例的 MNEMOTRACE_NEXT_TEST_POSTGRES_DSN")
    if options.out.exists() or not options.out.parent.is_dir():
        parser.error("--out 须使用已存在目录下的新文件路径")
    report: dict[str, object] = {
        "accepted": False,
        "reference_time": _AT,
        "models": {
            key: os.environ.get(f"MNEMOTRACE_NEXT_{key}")
            for key in (
                "LLM_MODEL",
                "LLM_JSON_MODE",
                "LLM_TEMPERATURE",
                "EMBED_MODEL",
                "EMBED_REVISION",
            )
        },
    }
    with options.out.open("x", encoding="utf-8") as output:
        try:
            accepted = asyncio.run(
                _run(dsn, report), loop_factory=asyncio.SelectorEventLoop
            )
        except (ValueError, psycopg.Error, *_MODEL_ERRORS) as exc:
            report["error"] = type(exc).__name__
            accepted = False
        json.dump(report, output, ensure_ascii=False, indent=2, default=asdict)
        output.write("\n")
    print(f"{'通过' if accepted else '未通过'}，报告 {options.out}")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
