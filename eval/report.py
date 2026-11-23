"""从逐题原始记录验证运行并重算质量和成本汇总。"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import ceil
from statistics import fmean

from pydantic import ValidationError

from .artifacts import Store
from .config import ALL_BACKENDS, Config, Model
from .contracts import Answer, ContextEvidence, ModelCall, Retrieval, Score
from .datasets import LONGMEMEVAL_CATEGORIES
from .http import EvaluationError
from .provenance import committed_fingerprint
from .scoring import (
    LONGMEMEVAL_REFERENCE_JUDGE,
    PROTOCOL_VERSION,
    retrieval_metrics,
    score_answer,
)


@dataclass
class _Usage:
    requests: int = 0
    measured: int = 0
    measured_inputs: int = 0
    measured_outputs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, call: ModelCall) -> None:
        self.requests += 1
        if call.prompt_tokens is not None:
            self.measured_inputs += 1
            self.input_tokens += call.prompt_tokens
        if call.completion_tokens is not None:
            self.measured_outputs += 1
            self.output_tokens += call.completion_tokens
        if call.prompt_tokens is not None and call.completion_tokens is not None:
            self.measured += 1

    def summary(self, model: Model | None) -> dict[str, int | float | None]:
        cost = None
        if (
            model is not None
            and model.input_usd_per_million is not None
            and model.output_usd_per_million is not None
            and (self.measured_inputs or self.measured_outputs or not self.requests)
        ):
            cost = (
                self.input_tokens * model.input_usd_per_million
                + self.output_tokens * model.output_usd_per_million
            ) / 1_000_000
        return {
            "requests": self.requests,
            "measured": self.measured,
            "prompt_tokens": self.input_tokens
            if self.measured_inputs == self.requests
            else None,
            "completion_tokens": self.output_tokens
            if self.measured_outputs == self.requests
            else None,
            "cost_usd": cost if self.measured == self.requests else None,
            "measured_prompt_tokens": self.input_tokens,
            "measured_completion_tokens": self.output_tokens,
            "measured_cost_usd": cost,
        }


@dataclass
class _Group:
    categories: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    headline: list[bool] = field(default_factory=list)
    abstentions: list[bool] = field(default_factory=list)
    retrieval: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    eligible: int = 0
    mapped: int = 0
    context_evidence: list[ContextEvidence] = field(default_factory=list)
    times: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    context_tokens: list[int] = field(default_factory=list)
    reader: _Usage = field(default_factory=_Usage)
    judge: _Usage = field(default_factory=_Usage)


def _accuracy(values: list[bool]) -> dict[str, int | float | None]:
    count = len(values)
    correct = sum(values)
    return {
        "correct": correct,
        "questions": count,
        "accuracy": correct / count if count else None,
    }


def _task_averaged_accuracy(categories: dict[str, list[bool]]) -> float | None:
    if not LONGMEMEVAL_CATEGORIES.issubset(categories):
        return None
    return fmean(
        fmean(categories[category]) for category in sorted(LONGMEMEVAL_CATEGORIES)
    )


def _context_evidence_summary(group: _Group) -> dict[str, int | float | None]:
    coverage = group.context_evidence
    complete = bool(coverage) and len(coverage) == group.eligible
    fractions = [
        len(item.covered_session_ids) / item.gold_session_count for item in coverage
    ]
    return {
        "eligible_questions": group.eligible,
        "mapped_questions": len(coverage),
        "any_complete_rate": fmean(value > 0 for value in fractions)
        if complete
        else None,
        "all_complete_rate": fmean(value == 1 for value in fractions)
        if complete
        else None,
        "mean_gold_session_coverage": fmean(fractions) if complete else None,
        "partially_covered_questions": sum(
            bool(item.partial_session_ids) for item in coverage
        )
        if complete
        else None,
    }


def _latency(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_seconds": ordered[ceil(len(ordered) * 0.5) - 1],
        "p95_seconds": ordered[ceil(len(ordered) * 0.95) - 1],
    }


def build_report(store: Store) -> dict[str, object]:
    """验证冻结后的题目覆盖，并基于原始记录生成报告。"""
    manifest = store.manifest
    config = manifest.config
    benchmark = config.benchmark
    cases = tuple(store.questions())
    errors = []
    if manifest.protocol_version != PROTOCOL_VERSION:
        raise EvaluationError("当前工具不能重算该评分协议版本")
    if (
        tuple(case.id for case in cases) != manifest.question_ids
        or len(cases) != manifest.expected_questions
    ):
        errors.append("实际题目清单与冻结清单不一致")
    expected = {
        (name, case.id, stage)
        for name in config.backends
        for case in cases
        for stage in ("retrieve", "answer", "score")
    }
    actual = store.stage_keys()
    if expected - actual:
        errors.append(f"缺少 {len(expected - actual)} 条阶段记录")
    if actual - expected:
        errors.append(f"出现 {len(actual - expected)} 条未声明的阶段记录")
    if pending := store.active():
        errors.append(f"有 {len(pending)} 个资源未确认清理")

    groups: dict[str, _Group] = {name: _Group() for name in config.backends}
    reader_models: set[str] = set()
    judge_models: set[str] = set()
    event_failures: Counter[str] = Counter()
    for event in store.events():
        name = event["backend"]
        stage = event["stage"]
        if event["kind"] == "failed":
            event_failures[f"{name}:{stage}"] += 1
        if event["kind"] == "succeeded" and stage == "ingest":
            value = event["value"]
            seconds = value.get("seconds") if isinstance(value, dict) else None
            if name in groups and isinstance(seconds, float):
                groups[name].times["ingest"].append(seconds)
        if event["kind"] == "model_call":
            if (name, event["subject"], stage) not in expected or stage not in {
                "answer",
                "score",
            }:
                errors.append("模型调用记录超出冻结的题目或阶段范围")
                continue
            try:
                call = ModelCall.model_validate(event["value"])
            except ValidationError:
                errors.append("模型调用记录无效")
                continue
            group = groups[name]
            (group.reader if stage == "answer" else group.judge).add(call)
    for name in config.backends:
        group = groups[name]
        for case in cases:
            if case.reference.has_retrieval_labels:
                group.eligible += 1
            try:
                retrieval_record, answer_record, score_record = (
                    store.get(name, case.id, stage)
                    for stage in ("retrieve", "answer", "score")
                )
                if retrieval_record is None:
                    continue
                retrieved = Retrieval.model_validate(retrieval_record.payload)
                durations = group.times
                durations["retrieve"].append(retrieval_record.seconds)
                if (
                    metrics := retrieval_metrics(benchmark, case, retrieved)
                ) is not None:
                    group.mapped += 1
                    for metric, value in metrics.items():
                        group.retrieval[metric].append(value)
                if answer_record is None:
                    continue
                answer = Answer.model_validate(answer_record.payload)
                durations["answer"].append(answer_record.seconds)
                durations["query_to_answer"].append(
                    retrieval_record.seconds + answer_record.seconds
                )
                group.context_tokens.append(answer.context_tokens)
                reader_models.add(answer.completion.model)
                if score_record is None:
                    continue
                stored = Score.model_validate(score_record.payload)
                score = score_answer(
                    benchmark,
                    case,
                    retrieved,
                    answer,
                    judge=stored.judge,
                    full=name == "full-context",
                )
                if score != stored:
                    errors.append(f"{name}/{case.id} 的评分与原始输出不一致")
                    continue
            except (EvaluationError, ValidationError):
                errors.append(f"{name}/{case.id} 的阶段记录无效")
                continue
            group.categories[score.category].append(score.correct)
            if score.context_evidence is not None:
                group.context_evidence.append(score.context_evidence)
            if not (benchmark == "locomo" and score.abstention):
                group.headline.append(score.correct)
            if score.abstention:
                group.abstentions.append(score.correct)
            durations["score"].append(score_record.seconds)
            if score.judge is not None:
                judge_models.add(score.judge.model)

    if len(reader_models) > 1 or len(judge_models) > 1:
        errors.append("回答或裁判响应出现不同实际模型版本")
    all_required_backends = set(config.backends) == set(ALL_BACKENDS)
    source_verified = (
        committed_fingerprint(manifest.source_commit) == manifest.code_sha256
    )
    publishable = (
        not errors
        and len(cases) == manifest.dataset.question_count
        and all_required_backends
        and source_verified
    )
    result: dict[str, object] = {
        "run_id": manifest.id,
        "benchmark": benchmark,
        "dataset_revision": manifest.dataset.revision,
        "dataset_questions": manifest.dataset.question_count,
        "dataset_files": manifest.dataset.files,
        "protocol_version": manifest.protocol_version,
        "source_commit": manifest.source_commit,
        "source_verified": source_verified,
        "code_sha256": manifest.code_sha256,
        "settings": manifest.public()["config"],
        "verdict_format": "option_letter"
        if benchmark == "personamem-v2"
        else "strict_yes_no",
        "complete": not errors,
        "publishable": publishable,
        "errors": errors,
        "all_required_backends": all_required_backends,
        "expected_questions_per_backend": manifest.expected_questions,
        "historical_stage_failures": dict(event_failures),
        "actual_reader_models": sorted(reader_models),
        "actual_judge_models": sorted(judge_models),
        "backends": {
            name: _group_summary(config, group) for name, group in groups.items()
        },
    }
    if benchmark == "longmemeval-s":
        result["longmemeval_judge"] = {
            "template": "upstream",
            "reference_model": LONGMEMEVAL_REFERENCE_JUDGE,
            "uses_reference_model": judge_models == {LONGMEMEVAL_REFERENCE_JUDGE},
        }
    if benchmark == "locomo":
        result["unmapped_evidence"] = [
            {"question_id": case.id, "labels": case.reference.unmapped_evidence}
            for case in cases
            if case.reference.unmapped_evidence
        ]
    return result


def _group_summary(config: Config, group: _Group) -> dict[str, object]:
    summary: dict[str, object] = {
        "qa": _accuracy(group.headline),
        "abstention": _accuracy(group.abstentions),
        "scored_questions": sum(map(len, group.categories.values())),
        "by_category": {
            category: _accuracy(values) for category, values in group.categories.items()
        },
        "retrieval": (
            {metric: fmean(values) for metric, values in group.retrieval.items()}
            if group.mapped and group.mapped == group.eligible
            else None
        ),
        "evidence_coverage": {"mapped": group.mapped, "eligible": group.eligible},
        "latency": {
            stage: _latency(values) for stage, values in group.times.items() if values
        },
        "mean_context_tokens": fmean(group.context_tokens)
        if group.context_tokens
        else None,
        "reader_usage": group.reader.summary(config.reader),
        "judge_usage": group.judge.summary(config.judge),
        "write_and_embedding_cost_usd": None,
    }
    if config.benchmark == "longmemeval-s":
        summary["task_averaged_accuracy"] = _task_averaged_accuracy(group.categories)
        summary["context_evidence_coverage"] = _context_evidence_summary(group)
    return summary
