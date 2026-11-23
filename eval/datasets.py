"""校验并加载公开数据集，构建时序历史和独立评分标注。"""

import ast
import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import file_digest, sha256
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, PositiveInt, TypeAdapter

from .contracts import Benchmark, Case, Query, Record, Reference, Role, Session, Turn

_RELEASES = Path(__file__).with_name("dataset-lock.json")
_LOCOMO_CATEGORIES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}
LONGMEMEVAL_CATEGORIES = frozenset(
    {
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "multi-session",
        "knowledge-update",
        "temporal-reasoning",
    }
)


class Release(Record):
    """记录上游修订、题量和各原始文件的校验和。"""

    revision: str
    question_count: PositiveInt
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class Dataset:
    """包含完整题目清单与已验证的输入历史。"""

    release: Release
    cases: tuple[Case, ...]
    histories: dict[str, tuple[Session, ...]]

    def select(self, question_ids: tuple[str, ...]) -> tuple[Case, ...]:
        """按原始数据顺序返回选定题目。"""
        if not question_ids:
            return self.cases
        wanted = set(question_ids)
        selected = tuple(case for case in self.cases if case.id in wanted)
        if len(selected) != len(wanted):
            raise ValueError("选择中含有未知的题目 ID")
        return selected


class _Upstream(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)


class _TextTurn(_Upstream):
    role: Role
    content: str


class _LongMemEval(_Upstream):
    question_id: str
    question_type: str
    question: str
    answer: JsonValue
    question_date: str
    haystack_session_ids: list[str]
    haystack_dates: list[str]
    haystack_sessions: list[list[_TextTurn]]
    answer_session_ids: list[str]


class _LoCoMoTurn(_Upstream):
    speaker: str
    dia_id: str
    text: str
    blip_caption: str | None = None


class _LoCoMoQuestion(_Upstream):
    question: str
    answer: str | None = None
    evidence: list[str]
    category: Literal[1, 2, 3, 4, 5]


class _LoCoMo(_Upstream):
    sample_id: str
    conversation: dict[str, JsonValue]
    qa: list[_LoCoMoQuestion]


class _PersonaHistory(_Upstream):
    chat_history: list[_TextTurn]


class _PersonaQuestion(_Upstream):
    chat_history_32k_link: str
    user_query: str
    correct_answer: str
    incorrect_answers: str
    pref_type: str


def _utc(value: str) -> str:
    """将两个公开基准中无时区的时间统一按 UTC 解释。"""
    if " on " in value:
        date = datetime.strptime(value.strip(), "%I:%M %p on %d %B, %Y")
    else:
        simple = re.sub(r" \([A-Za-z]+\)(?= )", "", value.strip()).replace("/", "-")
        date = datetime.fromisoformat(simple)
    return (
        date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    ).isoformat()


def _load_longmemeval(path: Path) -> tuple[list[Case], dict[str, tuple[Session, ...]]]:
    rows = TypeAdapter(list[_LongMemEval]).validate_json(path.read_bytes())
    histories = {}
    cases = []
    for row in rows:
        if row.question_type not in LONGMEMEVAL_CATEGORIES:
            raise ValueError("LongMemEval 包含未知题型")
        dates = [_utc(date) for date in row.haystack_dates]
        abstention = row.question_id.endswith("_abs")
        if not abstention and (
            not row.answer_session_ids
            or not set(row.answer_session_ids).issubset(row.haystack_session_ids)
        ):
            raise ValueError("LongMemEval 缺少可映射的证据会话")
        sessions = []
        for index, (session_id, date, messages) in enumerate(
            zip(row.haystack_session_ids, dates, row.haystack_sessions, strict=True)
        ):
            turns = tuple(
                Turn(f"{session_id}:{index}:{i}", message.role, message.content, date)
                for i, message in enumerate(messages)
            )
            sessions.append(Session(session_id, turns, date))
        # cleaned 发布仍有乱序会话，相同时间保留文件中的顺序。
        histories[row.question_id] = tuple(
            sorted(sessions, key=lambda session: session.event_time or "")
        )
        cases.append(
            Case(
                row.question_id,
                row.question_id,
                Query(row.question, _utc(row.question_date)),
                Reference(
                    row.question_type,
                    str(row.answer),
                    tuple(row.answer_session_ids),
                    abstention,
                ),
            )
        )
    return cases, histories


def _locomo_sessions(row: _LoCoMo) -> tuple[Session, ...]:
    conversation = row.conversation
    speaker_a, speaker_b = conversation["speaker_a"], conversation["speaker_b"]
    if (
        not isinstance(speaker_a, str)
        or not isinstance(speaker_b, str)
        or speaker_a == speaker_b
    ):
        raise ValueError("LoCoMo 说话人字段无效")
    numbers = sorted(
        int(key[8:]) for key in conversation if re.fullmatch(r"session_\d+", key)
    )
    turns_type = TypeAdapter(list[_LoCoMoTurn])
    date_type = TypeAdapter(str)
    sessions = []
    for number in numbers:
        key = f"session_{number}"
        date = _utc(date_type.validate_python(conversation[f"{key}_date_time"]))
        turns = []
        for turn in turns_type.validate_python(conversation[key]):
            if turn.speaker not in {speaker_a, speaker_b}:
                raise ValueError("LoCoMo 有未知说话人")
            text = turn.text
            if turn.blip_caption:
                text += f"\n[Image caption: {turn.blip_caption}]"
            turns.append(
                Turn(
                    turn.dia_id,
                    "user" if turn.speaker == speaker_a else "assistant",
                    text,
                    date,
                    turn.speaker,
                )
            )
        sessions.append(Session(f"D{number}", tuple(turns), date))
    return tuple(sessions)


def _locomo_evidence(
    labels: list[str], turn_ids: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    evidence: list[str] = []
    unmapped: list[str] = []
    for label in labels:
        ids = [part.strip() for part in re.split(r"[;,]", label) if part.strip()]
        if not ids or any(turn_id not in turn_ids for turn_id in ids):
            unmapped.append(label)
        evidence.extend(turn_id for turn_id in ids if turn_id in turn_ids)
    return tuple(dict.fromkeys(evidence)), tuple(dict.fromkeys(unmapped))


def _load_locomo(path: Path) -> tuple[list[Case], dict[str, tuple[Session, ...]]]:
    rows = TypeAdapter(list[_LoCoMo]).validate_json(path.read_bytes())
    histories = {}
    cases = []
    for row in rows:
        sessions = _locomo_sessions(row)
        histories[row.sample_id] = sessions
        turn_ids = {turn.id for session in sessions for turn in session.turns}
        for index, qa in enumerate(row.qa):
            if qa.category != 5 and qa.answer is None:
                raise ValueError("LoCoMo 可答题缺少答案")
            evidence, unmapped = _locomo_evidence(qa.evidence, turn_ids)
            cases.append(
                Case(
                    f"{row.sample_id}:{index:04d}",
                    row.sample_id,
                    Query(qa.question),
                    Reference(
                        _LOCOMO_CATEGORIES[qa.category],
                        qa.answer or "",
                        evidence,
                        qa.category == 5,
                        unmapped_evidence=unmapped,
                    ),
                )
            )
    return cases, histories


def _choice_options(
    case_id: str, correct: str, incorrect: list[str]
) -> tuple[tuple[str, ...], str]:
    options = [correct, *incorrect]
    if (
        len(options) != 4
        or len(set(options)) != 4
        or not all(option.strip() for option in options)
    ):
        raise ValueError("PersonaMem 需要四个不同的非空选项")
    options.sort(key=lambda option: sha256(f"{case_id}\0{option}".encode()).digest())
    return tuple(options), "ABCD"[options.index(correct)]


def _load_persona(
    paths: dict[str, Path],
) -> tuple[list[Case], dict[str, tuple[Session, ...]]]:
    histories: dict[str, tuple[Session, ...]] = {}
    cases = []
    with paths["benchmark/text/benchmark.csv"].open(
        encoding="utf-8-sig", newline=""
    ) as file:
        for index, raw in enumerate(csv.DictReader(file)):
            row = _PersonaQuestion.model_validate(raw)
            filename = row.chat_history_32k_link.rsplit("/", 1)[-1]
            history_path = f"data/chat_history_32k/{filename}"
            if history_path not in paths:
                raise ValueError("PersonaMem 历史不在发布锁中")
            if filename not in histories:
                turns = _PersonaHistory.model_validate_json(
                    paths[history_path].read_bytes()
                )
                histories[filename] = (
                    Session(
                        filename,
                        tuple(
                            Turn(f"{filename}:{i}", turn.role, turn.content)
                            for i, turn in enumerate(turns.chat_history)
                        ),
                    ),
                )
            case_id = f"persona:{index:04d}"
            incorrect = TypeAdapter(list[str]).validate_json(row.incorrect_answers)
            options, gold = _choice_options(case_id, row.correct_answer, incorrect)
            # 发布版 CSV 使用 Python 字面量保存问题字段。
            query = _TextTurn.model_validate(ast.literal_eval(row.user_query))
            cases.append(
                Case(
                    case_id,
                    filename,
                    Query(query.content, choices=options),
                    Reference(row.pref_type, row.correct_answer, correct_choice=gold),
                )
            )
    return cases, histories


def verified_files(root: Path, release: Release) -> dict[str, Path]:
    """验证锁定文件的路径归属与内容哈希，再向加载器开放。"""
    root = root.resolve()
    paths = {}
    for name, expected in release.files.items():
        relative = PurePosixPath(name)
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root):
            raise ValueError(f"数据路径超出配置目录 {name}")
        with path.open("rb") as file:
            if file_digest(file, "sha256").hexdigest() != expected:
                raise ValueError(f"数据文件校验和不匹配 {name}")
        paths[name] = path
    return paths


def load_dataset(name: Benchmark, root: Path) -> Dataset:
    """加载固定发布版本并检查完整题目数量和唯一性。"""
    releases = TypeAdapter(dict[str, Release]).validate_json(_RELEASES.read_bytes())
    release = releases[name]
    paths = verified_files(root, release)
    if name == "longmemeval-s":
        cases, histories = _load_longmemeval(paths["longmemeval_s_cleaned.json"])
        if sum(case.reference.abstention for case in cases) != 30:
            raise ValueError("LongMemEval 弃答题数量不符合发布协议")
    elif name == "locomo":
        cases, histories = _load_locomo(paths["locomo10.json"])
        if (
            len(histories) != 10
            or sum(case.reference.abstention for case in cases) != 446
        ):
            raise ValueError("LoCoMo 会话或对抗题数量不符合发布协议")
    else:
        cases, histories = _load_persona(paths)
    if len(cases) != release.question_count or len({case.id for case in cases}) != len(
        cases
    ):
        raise ValueError("数据集题目数量不符或 ID 重复")
    return Dataset(release, tuple(cases), histories)
