"""提供崩溃后可恢复的评测状态和可归档的原始记录。"""

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from tempfile import mkstemp
from typing import Self, TextIO, TypedDict
from uuid import uuid4

from filelock import FileLock
from pydantic import Field, JsonValue, TypeAdapter

from .config import Config
from .contracts import BackendName, Case, ModelCall, Record, Stage
from .datasets import Release

_RUN_ID = re.compile(r"[a-f0-9]{32}")
_CASE = TypeAdapter(Case)


class Manifest(Record):
    """固定一次运行使用的数据、代码和预期对照。"""

    id: str
    config: Config
    dataset: Release
    protocol_version: str
    code_sha256: str
    source_commit: str | None
    expected_questions: int
    question_ids: tuple[str, ...]

    def public(self) -> dict[str, JsonValue]:
        """导出可分享的配置和模型参数，隐去本地连接信息。"""
        data = self.model_dump(
            mode="json",
            exclude={
                "config": {
                    "data_dir": True,
                    **{
                        name: {"base_url", "key_env"}
                        for name in ("reader", "judge", "mnemotrace")
                    },
                }
            },
        )
        return TypeAdapter(dict[str, JsonValue]).validate_python(data)


class StageRecord(Record):
    """记录成功阶段的原始载荷和耗时。"""

    seconds: float = Field(ge=0, allow_inf_nan=False)
    payload: JsonValue


class _Event(TypedDict):
    sequence: int
    backend: str
    subject: str
    stage: str
    kind: str
    value: JsonValue


class Store:
    """用事务提交单题阶段结果和待回收资源。"""

    def __init__(
        self, directory: Path, db: sqlite3.Connection, manifest: Manifest
    ) -> None:
        self.directory = directory
        self._db = db
        self.manifest = manifest

    @classmethod
    @contextmanager
    def create(
        cls, root: Path, manifest: Manifest, cases: tuple[Case, ...]
    ) -> Iterator[Self]:
        """创建唯一运行目录并写入题目和配置快照。"""
        if not _RUN_ID.fullmatch(manifest.id):
            raise ValueError("运行 ID 无效")
        directory = root / manifest.id
        directory.mkdir(parents=True, exist_ok=False)
        with (
            FileLock(directory / ".lock", timeout=0),
            _connection(directory / "run.sqlite3") as db,
        ):
            db.executescript(
                """
                CREATE TABLE manifest(value TEXT NOT NULL);
                CREATE TABLE questions(id TEXT PRIMARY KEY, position INTEGER UNIQUE, value TEXT NOT NULL);
                CREATE TABLE stages(
                    backend TEXT NOT NULL, question_id TEXT NOT NULL REFERENCES questions(id),
                    stage TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (backend, question_id, stage)
                );
                CREATE TABLE resources(
                    backend TEXT NOT NULL, history_id TEXT NOT NULL, uncertain INTEGER NOT NULL,
                    PRIMARY KEY (backend, history_id)
                );
                CREATE TABLE events(
                    sequence INTEGER PRIMARY KEY, backend TEXT NOT NULL, subject TEXT NOT NULL,
                    stage TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL
                );
                """
            )
            with db:
                db.execute(
                    "INSERT INTO manifest VALUES (?)", (manifest.model_dump_json(),)
                )
                db.executemany(
                    "INSERT INTO questions VALUES (?, ?, ?)",
                    (
                        (case.id, i, _CASE.dump_json(case).decode())
                        for i, case in enumerate(cases)
                    ),
                )
            yield cls(directory, db, manifest)

    @classmethod
    @contextmanager
    def open(cls, directory: Path) -> Iterator[Self]:
        """独占已存在的运行目录，防止两个进程重复执行。"""
        directory = directory.resolve()
        if (
            not _RUN_ID.fullmatch(directory.name)
            or not (directory / "run.sqlite3").is_file()
            or any((directory / name).is_symlink() for name in ("run.sqlite3", ".lock"))
        ):
            raise ValueError("运行目录无效")
        with (
            FileLock(directory / ".lock", timeout=0),
            _connection(directory / "run.sqlite3") as db,
        ):
            rows = db.execute("SELECT value FROM manifest").fetchall()
            if len(rows) != 1:
                raise ValueError("运行清单缺失或重复")
            manifest = Manifest.model_validate_json(rows[0][0])
            if directory.name != manifest.id:
                raise ValueError("运行目录与清单 ID 不一致")
            yield cls(directory, db, manifest)

    def questions(self) -> Iterator[Case]:
        """按原始题目顺序读取冻结的清单。"""
        for (raw,) in self._db.execute("SELECT value FROM questions ORDER BY position"):
            yield _CASE.validate_json(raw)

    def get(
        self, backend: BackendName, question: str, stage: Stage
    ) -> StageRecord | None:
        """读取一个已成功提交的阶段。"""
        row = self._db.execute(
            "SELECT value FROM stages WHERE backend=? AND question_id=? AND stage=?",
            (backend, question, stage),
        ).fetchone()
        return StageRecord.model_validate_json(row[0]) if row is not None else None

    def put(
        self,
        backend: BackendName,
        question: str,
        stage: Stage,
        payload: Record,
        seconds: float,
    ) -> None:
        """原子保存唯一的成功阶段记录。"""
        value = StageRecord(
            seconds=seconds, payload=_record_payload(payload)
        ).model_dump_json()
        with self._db:
            self._db.execute(
                "INSERT INTO stages VALUES (?, ?, ?, ?)",
                (backend, question, stage, value),
            )

    def event(
        self,
        backend: BackendName,
        subject: str,
        stage: Stage,
        kind: str,
        value: JsonValue,
    ) -> None:
        """追加失败、写入或清理事件。"""
        with self._db:
            self._db.execute(
                "INSERT INTO events(backend, subject, stage, kind, value) VALUES (?, ?, ?, ?, ?)",
                (backend, subject, stage, kind, json.dumps(value, ensure_ascii=False)),
            )

    def active(self) -> set[tuple[BackendName, str]]:
        """返回需要清理的已登记资源。"""
        rows = self._db.execute("SELECT backend, history_id FROM resources").fetchall()
        return set(TypeAdapter(list[tuple[BackendName, str]]).validate_python(rows))

    def model_call(
        self, backend: BackendName, question: str, stage: Stage, call: ModelCall
    ) -> None:
        """追加模型响应，供审计和累计用量使用。"""
        self.event(backend, question, stage, "model_call", _record_payload(call))

    def acquire(
        self, backend: BackendName, history: str, *, uncertain: bool = False
    ) -> None:
        """在发出远端写入前登记待清理资源。"""
        with self._db:
            self._db.execute(
                "INSERT INTO resources VALUES (?, ?, ?)", (backend, history, uncertain)
            )

    def uncertain(self, backend: BackendName, history: str) -> bool:
        """判断远端写入是否仍可能在完成。"""
        return (
            self._db.execute(
                "SELECT 1 FROM resources WHERE backend=? AND history_id=? AND uncertain=1",
                (backend, history),
            ).fetchone()
            is not None
        )

    def confirm_ingest(self, backend: BackendName, history: str) -> None:
        """确认创建结果或同步写入请求已结束。"""
        with self._db:
            self._db.execute(
                "UPDATE resources SET uncertain=0 WHERE backend=? AND history_id=?",
                (backend, history),
            )

    def release(self, backend: BackendName, history: str) -> None:
        """清理成功后撤销资源登记。"""
        with self._db:
            self._db.execute(
                "DELETE FROM resources WHERE backend=? AND history_id=?",
                (backend, history),
            )

    def rewind(self, stage: str) -> None:
        """显式重跑作答或评分，保留此前的召回结果和事件。"""
        stages = {"answer": ("answer", "score"), "score": ("score",)}[stage]
        # 报告先失效，避免 SQLite 提交后进程中断而留下过期的已完成结论。
        self.write_json(
            "report.json",
            {
                "run_id": self.manifest.id,
                "complete": False,
                "publishable": False,
                "errors": ["阶段回退，等待重算报告"],
            },
        )
        with self._db:
            self._db.executemany(
                "DELETE FROM stages WHERE stage=?", ((key,) for key in stages)
            )

    def stage_keys(self) -> set[tuple[str, str, str]]:
        """读取实际提交记录的唯一键。"""
        return set(
            self._db.execute(
                "SELECT backend, question_id, stage FROM stages"
            ).fetchall()
        )

    def has(self, backend: BackendName, question: str, stage: Stage) -> bool:
        """查询阶段是否已提交，避免反序列化长上下文。"""
        return (
            self._db.execute(
                "SELECT 1 FROM stages WHERE backend=? AND question_id=? AND stage=?",
                (backend, question, stage),
            ).fetchone()
            is not None
        )

    def events(self) -> Iterator[_Event]:
        """按提交顺序读取事件。"""
        for seq, backend, subject, stage, kind, value in self._db.execute(
            "SELECT sequence, backend, subject, stage, kind, value FROM events ORDER BY sequence"
        ):
            yield {
                "sequence": seq,
                "backend": backend,
                "subject": subject,
                "stage": stage,
                "kind": kind,
                "value": json.loads(value),
            }

    def export(self) -> None:
        """逐行导出状态，公开清单不包含本地路径或端点。"""
        self.write_json("manifest.json", self.manifest.public())
        self._write_lines(
            "questions.jsonl", (asdict(case) for case in self.questions())
        )
        self._write_lines("events.jsonl", self.events())
        for stage in ("retrieve", "answer", "score"):
            rows = self._db.execute(
                "SELECT backend, question_id, value FROM stages WHERE stage=? ORDER BY backend, question_id",
                (stage,),
            )
            self._write_lines(
                f"{stage}.jsonl",
                (
                    {"backend": backend, "question_id": question, **json.loads(value)}
                    for backend, question, value in rows
                ),
            )

    def write_json(self, filename: str, value: object) -> None:
        """原子更新一个汇总 JSON 文件。"""
        with _atomic_export(self.directory / filename) as file:
            file.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _write_lines(self, filename: str, rows: Iterable[Mapping[str, object]]) -> None:
        with _atomic_export(self.directory / filename) as file:
            for row in rows:
                file.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


@contextmanager
def _atomic_export(path: Path) -> Iterator[TextIO]:
    descriptor, name = mkstemp(dir=path.parent, prefix=f".{path.name}-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            yield file
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _record_payload(record: Record) -> JsonValue:
    return TypeAdapter(JsonValue).validate_python(record.model_dump(mode="json"))


def run_id() -> str:
    """分配不依赖外部标识的安全运行 ID。"""
    return uuid4().hex


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        db.close()
