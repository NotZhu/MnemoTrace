"""验证评测在丢响应、降级和续跑时的可核验行为。"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import tiktoken
from filelock import Timeout

from eval import __main__ as cli
from eval import backends, provenance, report, runner
from eval.artifacts import Store
from eval.config import Config, Memory, Model
from eval.contracts import (
    Case,
    Evidence,
    ModelCall,
    Query,
    Reference,
    Retrieval,
    Score,
    Session,
    Turn,
)
from eval.datasets import LONGMEMEVAL_CATEGORIES, Dataset, Release
from eval.http import Chat, EvaluationError, Http
from eval.report import build_report
from eval.runner import (
    cleanup,
    execute,
    make_manifest,
    preflight,
    reader_for,
    resume_dataset,
)

_SPACE = "1" * 32
_OTHER_SPACE = "2" * 32
_SESSION = "3" * 32
_OP = "4" * 32
_MESSAGE = "5" * 32
_TIME = "2020-01-01T10:00:00+00:00"


@pytest.mark.parametrize("provider", [None, "deepseek"])
def test_chat_can_opt_out_of_deepseek_thinking(provider: str | None) -> None:
    """仅在显式配置时关闭 DeepSeek 思考，保留短裁判输出上限。"""
    config = Model.model_validate(
        {
            "base_url": "https://judge.example.test",
            "model": "judge",
            "max_output_tokens": 10,
            "disable_thinking": provider,
        }
    )

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 10
        if provider:
            assert body["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in body
        return httpx.Response(
            200, json={"model": "judge", "choices": [{"message": {"content": "yes"}}]}
        )

    calls: list[ModelCall] = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        chat = Chat(Http(client, "fixture", config), config)
        assert (
            chat.complete("Synthetic yes/no question", on_call=calls.append).text
            == "yes"
        )
    assert calls[0].status == 200


class _ByteEncoding:
    def encode(
        self, text: str, *, disallowed_special: tuple[str, ...] = ()
    ) -> list[int]:
        return list(text.encode())

    def decode(self, tokens: list[int], *, errors: str = "strict") -> str:
        return bytes(tokens).decode(errors=errors)


@pytest.fixture
def dataset() -> Dataset:
    """返回带有真实来源标记和独立金标的合成样本。"""
    return Dataset(
        Release(revision="fixture", question_count=1, files={}),
        (
            Case(
                "q1",
                "history",
                Query("Which format?", _TIME),
                Reference("single-session-user", "SECRET-GOLD", ("s1",)),
            ),
        ),
        {
            "history": (
                Session("s1", (Turn("t1", "user", "format XLSX", _TIME),), _TIME),
            )
        },
    )


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """配置本地假端点及无需下载的确定性 tokenizer。"""
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: _ByteEncoding())
    monkeypatch.setattr(backends, "sleep", lambda _: None)
    return Config(
        benchmark="longmemeval-s",
        data_dir=tmp_path,
        reader_context_tokens=2000,
        reader=Model(
            base_url="http://127.0.0.1:8001/v1", model="reader", max_output_tokens=5
        ),
        judge=Model(
            base_url="http://127.0.0.1:8002/v1", model="judge", max_output_tokens=5
        ),
        mnemotrace=Memory(
            base_url="http://127.0.0.1:8100",
            version="fixture",
            write_model="writer",
            embedding_model="embedder",
        ),
    )


@dataclass
class _Api:
    scope: str | None = None
    remember_keys: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    reader_prompts: list[str] = field(default_factory=list)
    fail_recall: bool = True
    fail_delete: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        port, path, method = request.url.port, request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if port in {8001, 8002}:
            assert path == "/v1/chat/completions"
            prompt = body["messages"][0]["content"]
            if port == 8001:
                assert "SECRET-GOLD" not in prompt
                self.reader_prompts.append(prompt)
            else:
                assert "SECRET-GOLD" in prompt
            return httpx.Response(
                200,
                json={
                    "model": "reader" if port == 8001 else "judge",
                    "choices": [
                        {
                            "message": {"content": "XLSX" if port == 8001 else "no"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 2},
                },
            )
        assert "SECRET-GOLD" not in request.content.decode()
        assert port == 8100
        if path == "/v1/spaces" and method == "POST":
            self.scope = body["name"]
            return httpx.Response(201, json={"space_id": _SPACE, "name": self.scope})
        if path == "/v1/spaces":
            if "cursor" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "items": [{"space_id": _OTHER_SPACE, "name": "unrelated"}],
                        "next_cursor": "next",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": [{"space_id": _SPACE, "name": self.scope}]
                    if self.scope
                    else [],
                    "next_cursor": None,
                },
            )
        if method == "DELETE":
            assert path == f"/v1/spaces/{_SPACE}"
            if self.fail_delete:
                return httpx.Response(503)
            self.deleted.append(path)
            self.scope = None
            return httpx.Response(204)
        if path.endswith("/sessions"):
            return httpx.Response(201, json={"session_id": _SESSION})
        if path.endswith("/remember"):
            self.remember_keys.append(request.headers["Idempotency-Key"])
            assert body == {
                "messages": [
                    {"role": "user", "content": "format XLSX", "event_time": _TIME}
                ]
            }
            if len(self.remember_keys) == 1:
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return httpx.Response(202, json={"operation_id": _OP, "status": "queued"})
        if path.endswith(f"/operations/{_OP}"):
            return httpx.Response(
                200,
                json={
                    "operation_id": _OP,
                    "status": "succeeded",
                    "message_ids": [_MESSAGE],
                },
            )
        assert path.endswith("/recall")
        assert body == {"query": "Which format?", "top_k": 20, "track_usage": False}
        flag = "embedding_timeout" if self.fail_recall else None
        return httpx.Response(
            200,
            json={
                "evidence": [{"text": "format XLSX", "source_message_ids": [_MESSAGE]}],
                "degraded_reason": flag,
            },
        )


def test_resume_keeps_successes_and_exports_recomputable_report(
    tmp_path: Path,
    config: Config,
    dataset: Dataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从丢响应与召回降级恢复，尝试仅清理本次创建的资源。"""
    api = _Api()
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    monkeypatch.setattr(runner, "source_commit", lambda _: None)
    manifest = make_manifest(config, dataset, cases)
    monkeypatch.setattr("eval.runner.load_dataset", lambda *_: dataset)
    with httpx.Client(transport=httpx.MockTransport(api.handle)) as client:
        with Store.create(tmp_path, manifest, cases) as store:
            directory = store.directory
            execute(store, dataset, reader, client)
            report = build_report(store)
            assert not report["complete"]
            assert api.remember_keys[0] == api.remember_keys[1]
            assert len(api.reader_prompts) == 2
            assert not store.active()
            assert api.deleted == [f"/v1/spaces/{_SPACE}"]
            with pytest.raises(Timeout), Store.open(directory):
                pytest.fail("不应允许第二个执行器")

        api.fail_recall = False
        with Store.open(directory) as store:
            execute(store, resume_dataset(store, config), reader, client)
            report = build_report(store)
            assert report["complete"] and not report["publishable"]
            assert not report["source_verified"]
            assert len(api.reader_prompts) == 3
            assert len(api.remember_keys) == 3
            assert len(api.deleted) == 2 and api.scope is None
            store.export()
            store.write_json("report.json", report)
            persisted = json.loads(
                (directory / "report.json").read_text(encoding="utf-8")
            )
            assert (
                persisted["backends"]["mnemotrace"]["retrieval"]["recall_all@5"] == 1.0
            )
            assert set(persisted["backends"]) == {"mnemotrace", "full-context", "bm25"}
            assert (
                persisted["backends"]["full-context"]["context_evidence_coverage"][
                    "all_complete_rate"
                ]
                == 1.0
            )
            assert persisted["backends"]["full-context"]["retrieval"] is None
            assert (
                persisted["backends"]["full-context"]["task_averaged_accuracy"] is None
            )
            assert persisted["backends"]["full-context"]["qa"] == {
                "correct": 0,
                "questions": 1,
                "accuracy": 0.0,
            }
            assert (
                persisted["backends"]["full-context"]["reader_usage"]["measured"] == 1
            )
            assert "base_url" not in (directory / "manifest.json").read_text(
                encoding="utf-8"
            )
            assert (
                sum(1 for _ in (directory / "score.jsonl").open(encoding="utf-8")) == 3
            )
            with pytest.raises(EvaluationError, match="配置"):
                resume_dataset(store, config.model_copy(update={"top_k": 25}))
            first = store.get("mnemotrace", "q1", "retrieve")
            assert first is not None
            with pytest.raises(sqlite3.IntegrityError):
                store.put(
                    "mnemotrace",
                    "q1",
                    "retrieve",
                    Retrieval.model_validate(first.payload),
                    0,
                )


def test_cleanup_pending_and_full_context_preflight(
    tmp_path: Path,
    config: Config,
    dataset: Dataset,
) -> None:
    """预检拒绝截断的全文，失败清理保留待重试资源。"""
    tiny = config.model_copy(update={"reader_context_tokens": 50})
    with pytest.raises(EvaluationError, match="全文"):
        preflight(tiny, dataset, reader_for(tiny))
    api = _Api(fail_recall=False, fail_delete=True)
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    with (
        httpx.Client(transport=httpx.MockTransport(api.handle)) as client,
        Store.create(tmp_path, make_manifest(config, dataset, cases), cases) as store,
    ):
        execute(store, dataset, reader, client)
        assert store.active() == {("mnemotrace", "history")}
        assert not build_report(store)["complete"]
        api.fail_delete = False
        cleanup(store, client)
        assert not store.active() and build_report(store)["complete"]


def test_context_evidence_reports_cutoff_from_actual_input(
    tmp_path: Path, config: Config, dataset: Dataset
) -> None:
    """排名可命中全部金标，回答上下文只计完整保留的候选。"""
    config = config.model_copy(update={"backends": ("bm25",), "evidence_tokens": 15})
    reader = reader_for(config)
    case = Case(
        "q1",
        "history",
        dataset.cases[0].query,
        Reference("multi-session", "SECRET-GOLD", ("first", "second", "third")),
    )
    dataset = Dataset(dataset.release, (case,), dataset.histories)
    retrieved = Retrieval(
        evidence=(
            Evidence(text="full item", session_ids=("first",)),
            Evidence(text="cut inside candidate", session_ids=("second",)),
            Evidence(text="out of budget", session_ids=("third",)),
        ),
        provenance=True,
    )
    api = _Api()
    with (
        httpx.Client(transport=httpx.MockTransport(api.handle)) as client,
        Store.create(
            tmp_path, make_manifest(config, dataset, (case,)), (case,)
        ) as store,
    ):
        store.put("bm25", "q1", "retrieve", retrieved, 0.1)
        execute(store, dataset, reader, client)
        result = json.loads(json.dumps(build_report(store)))
        assert result["complete"]
        group = result["backends"]["bm25"]
        assert group["retrieval"]["recall_all@5"] == 1.0
        assert group["context_evidence_coverage"] == {
            "eligible_questions": 1,
            "mapped_questions": 1,
            "any_complete_rate": 1.0,
            "all_complete_rate": 0.0,
            "mean_gold_session_coverage": 1 / 3,
            "partially_covered_questions": 1,
        }
        assert "Evidence:\nfull item\n\ncut \n\nQuestion:" in api.reader_prompts[0]
        saved = store.get("bm25", "q1", "score")
        assert saved is not None
        coverage = Score.model_validate(saved.payload).context_evidence
        assert coverage is not None
        assert coverage.covered_session_ids == ("first",)
        assert coverage.partial_session_ids == ("second",)


def test_personamem_bm25_ranks_source_chunks_per_query(config: Config) -> None:
    """同份长历史的前后段按问题命中不同候选，保留原始发言 ID。"""
    config = config.model_copy(update={"benchmark": "personamem-v2"})
    history = (
        Session(
            "history",
            (
                Turn("first", "user", "Mercury " + "a" * 450),
                Turn("long", "assistant", "夏" * 360),
                Turn("last", "user", "Neptune " + "z" * 460),
            ),
        ),
    )
    with httpx.Client() as client:
        bm25 = backends.build_backend("bm25", client, config)
        prepared = bm25.ingest("", history)
        beginning = bm25.retrieve(prepared, "Mercury", 1)
        ending = bm25.retrieve(prepared, "Neptune", 1)
        all_chunks = bm25.retrieve(prepared, "", 20).evidence
        original = backends.build_backend("full-context", client, config)
        full = original.retrieve(original.ingest("", history), "Neptune", 1).evidence

    assert beginning.evidence[0].turn_ids == ("first",)
    assert ending.evidence[0].turn_ids == ("last",)
    assert all(len(item.text.encode()) <= 512 for item in all_chunks)
    assert sum(item.text.count("夏") for item in all_chunks) == 360
    assert all("long" in item.turn_ids for item in all_chunks if "夏" in item.text)
    assert len(full) == 1 and full[0].turn_ids == ("first", "long", "last")


def test_locomo_reports_unmapped_labels_without_losing_qa(
    tmp_path: Path, config: Config, dataset: Dataset
) -> None:
    """无法映射的证据单列，题目仍计入问答分母。"""
    config = config.model_copy(update={"benchmark": "locomo", "backends": ("bm25",)})
    original = dataset.cases[0]
    case = Case(
        original.id,
        original.history_id,
        original.query,
        Reference("single-hop", "SECRET-GOLD", ("t1",), unmapped_evidence=("t1; t9",)),
    )
    dataset = Dataset(dataset.release, (case,), dataset.histories)
    api = _Api()
    reader = reader_for(config)
    with (
        httpx.Client(transport=httpx.MockTransport(api.handle)) as client,
        Store.create(
            tmp_path, make_manifest(config, dataset, (case,)), (case,)
        ) as store,
    ):
        execute(store, dataset, reader, client)
        result = json.loads(json.dumps(build_report(store)))
    assert result["complete"]
    assert result["unmapped_evidence"] == [
        {"question_id": case.id, "labels": ["t1; t9"]}
    ]
    assert result["backends"]["bm25"]["qa"]["questions"] == 1
    assert result["backends"]["bm25"]["retrieval"] is None
    assert result["backends"]["bm25"]["evidence_coverage"]["eligible"] == 0


@pytest.mark.parametrize(
    ("all_backends", "source_status"),
    [(False, "verified"), (True, "verified"), (True, "missing"), (True, "different")],
)
def test_longmemeval_report_requires_three_groups_and_macro_average(
    tmp_path: Path,
    config: Config,
    dataset: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    all_backends: bool,
    source_status: str,
) -> None:
    """验证完整性和发布门槛，并区分题目加权与题型等权正确率。"""
    if not all_backends:
        config = config.model_copy(update={"backends": ("mnemotrace", "full-context")})
    categories = sorted(LONGMEMEVAL_CATEGORIES)
    cases = tuple(
        Case(
            f"q{index}",
            "history",
            dataset.cases[0].query,
            Reference(category, "SECRET-GOLD", ("s1",)),
        )
        for index, category in enumerate((*categories, categories[0]))
    )
    release = dataset.release.model_copy(update={"question_count": len(cases)})
    dataset = Dataset(release, cases, dataset.histories)
    source = None if source_status == "missing" else "a" * 40
    monkeypatch.setattr(runner, "source_commit", lambda _: source)
    committed = {
        "verified": provenance.code_fingerprint(),
        "missing": None,
        "different": "other-fingerprint",
    }[source_status]
    monkeypatch.setattr(report, "committed_fingerprint", lambda _: committed)
    publishable = all_backends and source_status == "verified"

    api = _Api(fail_recall=False)
    judge_calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal judge_calls
        response = api.handle(request)
        if request.url.port != 8002:
            return response
        body = response.json()
        body["choices"][0]["message"]["content"] = (
            "yes" if judge_calls % len(cases) in {0, len(cases) - 1} else "no"
        )
        judge_calls += 1
        return httpx.Response(200, json=body)

    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        Store.create(tmp_path, make_manifest(config, dataset, cases), cases) as store,
    ):
        execute(store, dataset, reader, client)
        result = json.loads(json.dumps(build_report(store)))
        assert result["complete"] and result["publishable"] == publishable
        assert result["source_verified"] == (source_status == "verified")
        assert result["all_required_backends"] == all_backends
        assert result["verdict_format"] == "strict_yes_no"
        assert result["actual_judge_models"] == ["judge"]
        assert result["longmemeval_judge"] == {
            "template": "upstream",
            "reference_model": "gpt-4o-2024-08-06",
            "uses_reference_model": False,
        }
        assert result["settings"]["top_k"] == config.top_k
        assert result["settings"]["evidence_tokens"] == config.evidence_tokens
        assert result["settings"]["mnemotrace"]["version"] == "fixture"
        assert "base_url" not in json.dumps(result["settings"])
        assert "key_env" not in json.dumps(result["settings"])
        full = result["backends"]["full-context"]
        assert full["qa"]["accuracy"] == pytest.approx(2 / 7)
        assert full["task_averaged_accuracy"] == pytest.approx(1 / 6)
        assert full["scored_questions"] == len(cases)
        assert full["write_and_embedding_cost_usd"] is None
        directory = store.directory

    monkeypatch.setattr(cli, "_RUNS", tmp_path)
    args = ["eval", "validate", "--run-dir", str(directory)]
    monkeypatch.setattr("sys.argv", args)
    assert cli.main() == 0
    monkeypatch.setattr("sys.argv", [*args, "--for-publication"])
    assert cli.main() == (0 if publishable else 1)


def test_cli_offline_commands_do_not_create_http_clients(
    tmp_path: Path,
    config: Config,
    dataset: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """预检、校验和报告通过本地入口执行，且不会连接服务。"""
    run_root = tmp_path / "runs"
    monkeypatch.setattr(cli, "_RUNS", run_root)
    monkeypatch.setattr(cli, "load_dataset", lambda *_: dataset)
    monkeypatch.setattr(
        httpx, "Client", lambda **_: pytest.fail("离线命令创建了 HTTP 客户端")
    )
    file = tmp_path / "fixture.toml"
    file.write_text(
        'benchmark = "longmemeval-s"\ndata_dir = "."\nbackends = ["full-context"]\n'
        'reader_context_tokens = 2000\n[reader]\nbase_url = "http://127.0.0.1:8001/v1"\n'
        'model = "reader"\n[judge]\nbase_url = "http://127.0.0.1:8002/v1"\nmodel = "judge"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["eval", "prepare", "--config", str(file)])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["question_ids"] == ["q1"]
    with Store.create(
        run_root, make_manifest(config, dataset, dataset.cases), dataset.cases
    ) as store:
        directory = store.directory
    for command in ("validate", "report"):
        monkeypatch.setattr("sys.argv", ["eval", command, "--run-dir", str(directory)])
        assert cli.main() == 1
        assert not json.loads(capsys.readouterr().out)["complete"]
    assert (directory / "report.json").is_file()


@pytest.mark.parametrize("status", [None, 503])
@pytest.mark.parametrize("lose_delete_response", [False, True])
def test_mnemotrace_cleanup_recovers_late_space(
    tmp_path: Path,
    config: Config,
    dataset: Dataset,
    status: int | None,
    lose_delete_response: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建状态未知且首次列表为空时，保留登记直到迟到的空间被删除。"""
    config = config.model_copy(update={"backends": ("mnemotrace", "full-context")})
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    manifest = make_manifest(config, dataset, cases)
    api = _Api(fail_recall=False)
    scope = backends.scoped_name(manifest.id, "history")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/spaces" and request.method == "POST":
            assert json.loads(request.content) == {"name": scope}
            if status is None:
                raise httpx.ReadTimeout("creation outcome unknown", request=request)
            return httpx.Response(status, json={"code": "commit_outcome_unknown"})
        response = api.handle(request)
        if request.method == "DELETE" and lose_delete_response:
            raise httpx.ReadTimeout(
                "deletion completed but response lost", request=request
            )
        return response

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        Store.create(tmp_path, manifest, cases) as store,
    ):
        execute(store, dataset, reader, client)
        assert store.active() == {("mnemotrace", "history")}
        assert not api.deleted
        directory = store.directory
    client_factory = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_: client_factory(transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(cli, "_RUNS", tmp_path)
    monkeypatch.setattr("sys.argv", ["eval", "cleanup", "--run-dir", str(directory)])
    assert cli.main() == 1
    api.scope = scope
    if lose_delete_response:
        assert cli.main() == 1
    assert cli.main() == 0
    with Store.open(directory) as store:
        assert not store.active() and api.deleted == [f"/v1/spaces/{_SPACE}"]


def test_cli_failed_rewind_invalidates_previous_report(
    tmp_path: Path, config: Config, dataset: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重跑回退后执行异常时，已完成的旧报告立即失效。"""
    config = config.model_copy(update={"backends": ("full-context",)})
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    monkeypatch.setattr(cli, "_RUNS", tmp_path)
    monkeypatch.setattr("eval.runner.load_dataset", lambda *_: dataset)
    with (
        httpx.Client(transport=httpx.MockTransport(_Api().handle)) as client,
        Store.create(tmp_path, make_manifest(config, dataset, cases), cases) as store,
    ):
        execute(store, dataset, reader, client)
        report = build_report(store)
        assert report["complete"]
        store.write_json("report.json", report)
        directory = store.directory

    path = directory / "report.json"

    def fail(*_: object) -> None:
        assert not json.loads(path.read_text(encoding="utf-8"))["complete"]
        raise ValueError("模拟回退后的启动异常")

    monkeypatch.setattr(cli, "execute", fail)
    monkeypatch.setattr(
        "sys.argv",
        ["eval", "resume", "--run-dir", str(directory), "--from-stage", "answer"],
    )
    assert cli.main() == 2
    report = json.loads(path.read_text(encoding="utf-8"))
    assert not report["complete"] and not report["publishable"]
    assert report["errors"] == ["缺少 2 条阶段记录"]
    assert (directory / "answer.jsonl").read_text(encoding="utf-8") == ""


def test_rewind_keeps_raw_model_responses_and_total_usage(
    tmp_path: Path, config: Config, dataset: Dataset
) -> None:
    """用最新有效裁判结果计分，累计截断、无效裁判与成功调用的实际用量。"""
    prices = {"input_usd_per_million": 1.0, "output_usd_per_million": 2.0}
    assert config.judge is not None
    config = config.model_copy(
        update={
            "backends": ("full-context",),
            "reader": config.reader.model_copy(update=prices),
            "judge": config.judge.model_copy(update=prices),
        }
    )
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    responses = iter(
        (
            ("reader", "first answer", "stop", 20),
            ("judge", "no", "stop", 30),
            ("reader", "partial answer", "length", 40),
            ("reader", "latest answer", "stop", 50),
            ("judge", "invalid verdict", "stop", 60),
            ("judge", "yes", "stop", 70),
        )
    )

    def handle(request: httpx.Request) -> httpx.Response:
        model, answer, reason, tokens = next(responses)
        assert request.url.port == (8001 if model == "reader" else 8002)
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": answer}, "finish_reason": reason}],
                "usage": {"prompt_tokens": tokens, "completion_tokens": 2},
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        Store.create(tmp_path, make_manifest(config, dataset, cases), cases) as store,
    ):
        execute(store, dataset, reader, client)
        assert build_report(store)["complete"]
        store.rewind("answer")
        for _ in range(3):
            execute(store, dataset, reader, client)
        report = json.loads(json.dumps(build_report(store)))
        assert report["complete"]
        group = report["backends"]["full-context"]
        assert group["qa"] == {"correct": 1, "questions": 1, "accuracy": 1.0}
        for role, tokens in (("reader", 110), ("judge", 160)):
            usage = group[f"{role}_usage"]
            assert usage["requests"] == usage["measured"] == 3
            assert usage["prompt_tokens"] == tokens and usage["completion_tokens"] == 6
            assert usage["cost_usd"] == pytest.approx((tokens + 12) / 1_000_000)
        store.export()
        events = (store.directory / "events.jsonl").read_text(encoding="utf-8")
        calls = [
            event["value"]
            for line in events.splitlines()
            if (event := json.loads(line))["kind"] == "model_call"
        ]
        assert [call["raw"]["choices"][0]["message"]["content"] for call in calls] == [
            "first answer",
            "no",
            "partial answer",
            "latest answer",
            "invalid verdict",
            "yes",
        ]


def test_cost_reports_known_usage_when_model_attempts_lack_totals(
    tmp_path: Path, config: Config, dataset: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺用量的请求保留在流水中，已返回的部分用量仍计费且脱敏。"""
    key = "fixture-key-12345"
    monkeypatch.setenv("EVAL_FIXTURE_READER_KEY", key)
    config = config.model_copy(
        update={
            "backends": ("full-context",),
            "reader": config.reader.model_copy(
                update={
                    "key_env": "EVAL_FIXTURE_READER_KEY",
                    "input_usd_per_million": 1.0,
                    "output_usd_per_million": 1.0,
                }
            ),
        }
    )
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        if request.url.port == 8002:
            return _Api().handle(request)
        assert request.headers["Authorization"] == f"Bearer {key}"
        requests += 1
        if requests == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        if requests == 2:
            return httpx.Response(
                503,
                json={"message": f"failed for {key}", "usage": {"prompt_tokens": 10}},
            )
        return _Api().handle(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        Store.create(tmp_path, make_manifest(config, dataset, cases), cases) as store,
    ):
        for _ in range(3):
            execute(store, dataset, reader, client)
        report = json.loads(json.dumps(build_report(store)))
        assert report["complete"]
        usage = report["backends"]["full-context"]["reader_usage"]
        assert usage["requests"] == 3 and usage["measured"] == 1
        assert usage["cost_usd"] is None and usage["prompt_tokens"] is None
        assert (
            usage["measured_prompt_tokens"] == 30
            and usage["measured_completion_tokens"] == 2
        )
        assert usage["measured_cost_usd"] == pytest.approx(32 / 1_000_000)
        calls = [
            event["value"]
            for event in store.events()
            if event["kind"] == "model_call" and event["stage"] == "answer"
        ]
        assert (
            isinstance(calls[0], dict)
            and calls[0]["status"] is None
            and calls[0]["raw"] is None
        )
        assert isinstance(calls[1], dict) and calls[1]["status"] == 503
        assert key not in json.dumps(calls) and "failed for [redacted]" in json.dumps(
            calls
        )
