"""协调已授权评测的写入、召回、作答、评分和资源回收。"""

from collections import defaultdict
from functools import partial
from time import monotonic

import httpx

from .artifacts import Manifest, Store, run_id
from .backends import (
    Backend,
    MnemoTrace,
    OriginalHistory,
    build_backend,
    scoped_name,
    validate_history,
)
from .config import Config
from .contracts import Answer, BackendName, Case, Retrieval, Session, Stage
from .datasets import Dataset, load_dataset
from .http import Chat, EvaluationError, Http
from .provenance import code_fingerprint, source_commit
from .scoring import PROTOCOL_VERSION, Reader, judge_prompt, score_answer


def reader_for(config: Config) -> Reader:
    """用配置的输出余量计算可供提示词使用的窗口。"""
    return Reader(
        config.tokenizer,
        config.evidence_tokens,
        config.reader_context_tokens - config.reader.max_output_tokens,
    )


def preflight(config: Config, dataset: Dataset, reader: Reader) -> tuple[Case, ...]:
    """验证选题、正文尺寸及全文组的模型预算，不调用模型。"""
    selected = dataset.select(config.question_ids)
    original = OriginalHistory("full-context")
    previous = ""
    full_context = ""
    verified: set[str] = set()
    for case in selected:
        if "mnemotrace" in config.backends and len(case.query.text) > 4096:
            raise EvaluationError(f"题目 {case.id} 超出 MnemoTrace 查询字符上限")
        if case.history_id not in verified:
            try:
                validate_history(dataset.histories[case.history_id], config)
            except EvaluationError as error:
                raise EvaluationError(
                    f"历史 {case.history_id} 无法写入：{error}"
                ) from None
            verified.add(case.history_id)
        if "full-context" in config.backends and case.history_id != previous:
            prepared = original.ingest("", dataset.histories[case.history_id])
            full_context = reader.context(
                original.retrieve(prepared, case.query.text, config.top_k).evidence,
                full=True,
            )
            previous = case.history_id
        try:
            reader.prompt(case.query, full_context)
        except EvaluationError:
            raise EvaluationError(f"题目 {case.id} 的全文请求超出模型窗口") from None
    return selected


def make_manifest(
    config: Config, dataset: Dataset, cases: tuple[Case, ...]
) -> Manifest:
    """冻结本次运行的代码、输入和题目顺序。"""
    fingerprint = code_fingerprint()
    return Manifest(
        id=run_id(),
        config=config,
        dataset=dataset.release,
        protocol_version=PROTOCOL_VERSION,
        code_sha256=fingerprint,
        source_commit=source_commit(fingerprint),
        expected_questions=len(cases),
        question_ids=tuple(case.id for case in cases),
    )


def resume_dataset(store: Store, config: Config) -> Dataset:
    """只有代码、配置、锁定数据和题目保持一致时恢复。"""
    manifest = store.manifest
    if config != manifest.config or code_fingerprint() != manifest.code_sha256:
        raise EvaluationError("评测配置或代码已变化，须创建新的运行")
    dataset = load_dataset(config.benchmark, config.data_dir)
    if dataset.release != manifest.dataset or tuple(
        store.questions()
    ) != dataset.select(config.question_ids):
        raise EvaluationError("数据版本或题目清单已变化")
    return dataset


def _failed(
    store: Store, name: BackendName, subject: str, stage: Stage, error: EvaluationError
) -> None:
    store.event(name, subject, stage, "failed", {"error": str(error), "raw": error.raw})


def _cleanup(
    store: Store,
    backend: MnemoTrace,
    history_id: str,
    *,
    confirm_uncertain: bool = False,
) -> None:
    scope = scoped_name(store.manifest.id, history_id)
    try:
        backend.cleanup(
            scope, on_found=partial(store.confirm_ingest, backend.name, history_id)
        )
    except EvaluationError as exc:
        _failed(store, backend.name, history_id, "cleanup", exc)
    else:
        if not confirm_uncertain and store.uncertain(backend.name, history_id):
            store.event(
                backend.name, history_id, "cleanup", "unconfirmed", {"scope": scope}
            )
            return
        store.release(backend.name, history_id)
        store.event(backend.name, history_id, "cleanup", "succeeded", {"scope": scope})


def cleanup(
    store: Store, client: httpx.Client, *, confirm_uncertain: bool = False
) -> None:
    """按登记清除该运行的资源，包括中断留下的资源。"""
    config = store.manifest.config
    histories = {case.history_id for case in store.questions()}
    for name, history_id in sorted(store.active()):
        if (
            name != "mnemotrace"
            or name not in config.backends
            or history_id not in histories
        ):
            raise EvaluationError("资源登记超出冻结的运行范围")
        try:
            backend = MnemoTrace.from_config(client, config)
        except ValueError:
            _failed(
                store,
                name,
                history_id,
                "cleanup",
                EvaluationError("后端凭据配置不可用"),
            )
            continue
        _cleanup(store, backend, history_id, confirm_uncertain=confirm_uncertain)


def _retrieve_history(
    store: Store,
    backend: Backend,
    history: tuple[Session, ...],
    history_id: str,
    cases: list[Case],
) -> None:
    name = backend.name
    missing = [case for case in cases if not store.has(name, case.id, "retrieve")]
    if not missing:
        return
    remote = backend if isinstance(backend, MnemoTrace) else None
    if (name, history_id) in store.active():
        return
    scope = scoped_name(store.manifest.id, history_id)
    if remote is not None:
        store.acquire(name, history_id, uncertain=True)
    try:
        start = monotonic()
        try:
            if remote is not None:
                prepared = remote.ingest(
                    scope,
                    history,
                    on_created=partial(store.confirm_ingest, name, history_id),
                )
            else:
                prepared = backend.ingest(scope, history)
        except EvaluationError as exc:
            _failed(store, name, history_id, "ingest", exc)
            return
        store.event(
            name,
            history_id,
            "ingest",
            "succeeded",
            {
                "scope": scope,
                "seconds": monotonic() - start,
                "remote_id": prepared.remote_id,
                "source_ids": {
                    key: list(source) for key, source in prepared.source_ids.items()
                },
                "receipts": prepared.receipts,
            },
        )
        for case in missing:
            start = monotonic()
            try:
                retrieved = backend.retrieve(
                    prepared, case.query.text, store.manifest.config.top_k
                )
            except EvaluationError as exc:
                _failed(store, name, case.id, "retrieve", exc)
            else:
                store.put(name, case.id, "retrieve", retrieved, monotonic() - start)
    finally:
        if remote is not None:
            _cleanup(store, remote, history_id)


def _answer_and_score(
    store: Store,
    name: BackendName,
    case: Case,
    reader: Reader,
    chat: Chat,
    judge: Chat | None,
) -> None:
    if store.has(name, case.id, "score"):
        return
    entry = store.get(name, case.id, "retrieve")
    if entry is None:
        return
    retrieved = Retrieval.model_validate(entry.payload)
    cached = store.get(name, case.id, "answer")
    if cached is None:
        start = monotonic()
        try:
            answer = reader.answer(
                chat,
                case.query,
                retrieved,
                full=name == "full-context",
                on_call=partial(store.model_call, name, case.id, "answer"),
            )
        except EvaluationError as exc:
            _failed(store, name, case.id, "answer", exc)
            return
        store.put(name, case.id, "answer", answer, monotonic() - start)
    else:
        answer = Answer.model_validate(cached.payload)
    start = monotonic()
    benchmark = store.manifest.config.benchmark
    try:
        judge_response = (
            judge.complete(
                judge_prompt(benchmark, case, answer.completion.text),
                on_call=partial(store.model_call, name, case.id, "score"),
            )
            if judge is not None
            else None
        )
        score = score_answer(
            benchmark,
            case,
            retrieved,
            answer,
            judge=judge_response,
            full=name == "full-context",
        )
    except EvaluationError as exc:
        _failed(store, name, case.id, "score", exc)
    else:
        store.put(name, case.id, "score", score, monotonic() - start)


def execute(
    store: Store, dataset: Dataset, reader: Reader, client: httpx.Client
) -> None:
    """运行缺失阶段，成功、失败或中断时均尝试回收资源。"""
    config = store.manifest.config
    cases = tuple(store.questions())
    histories: dict[str, list[Case]] = defaultdict(list)
    for case in cases:
        histories[case.history_id].append(case)
    cleanup(store, client)
    try:
        chat = Chat(Http(client, "回答模型", config.reader), config.reader)
        judge = (
            Chat(Http(client, "裁判模型", config.judge), config.judge)
            if config.judge is not None and config.benchmark != "personamem-v2"
            else None
        )
        for name in config.backends:
            needs_retrieval = any(
                not store.has(name, case.id, "retrieve") for case in cases
            )
            if needs_retrieval:
                backend = build_backend(name, client, config)
                for history_id, questions in histories.items():
                    _retrieve_history(
                        store,
                        backend,
                        dataset.histories[history_id],
                        history_id,
                        questions,
                    )
            for case in cases:
                _answer_and_score(store, name, case, reader, chat, judge)
    finally:
        cleanup(store, client)
