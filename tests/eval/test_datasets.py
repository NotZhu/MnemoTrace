"""用合成数据验证公开发布协议、输入校验和可复算评分。"""

import csv
import json
from hashlib import sha256
from pathlib import Path

import pytest

from eval import datasets
from eval.contracts import (
    Answer,
    Case,
    Completion,
    Evidence,
    Query,
    Reference,
    Retrieval,
)
from eval.scoring import judge_prompt, retrieval_metrics, score_answer


def _put(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value).encode()
    path.write_bytes(payload)
    return sha256(payload).hexdigest()


def _answer(text: str, *, context: str = "") -> Answer:
    return Answer(
        context=context,
        prompt=context,
        context_tokens=0,
        completion=Completion(text=text, model="reader"),
    )


def test_locomo_recall_cuts_at_candidate_rank() -> None:
    """长会话的后段证据仍属于该候选，排名第 11 的候选不进入前 10。"""
    ranked = Retrieval(
        evidence=(
            Evidence(
                text="long session", turn_ids=tuple(f"D1:{i}" for i in range(1, 13))
            ),
            *(Evidence(text="filler", turn_ids=(f"D{i}:1",)) for i in range(2, 11)),
            Evidence(text="outside top ten", turn_ids=("D11:1",)),
        ),
        provenance=True,
    )
    case = Case("q", "h", Query("question"), Reference("1", "answer", ("D1:12",)))
    scores = retrieval_metrics("locomo", case, ranked)
    assert scores is not None
    assert scores["recall_all@10"] == scores["recall_any@10"] == 1.0
    assert scores["ndcg_any@10"] == 1.0
    case = Case(
        "q", "h", Query("question"), Reference("1", "answer", ("D1:12", "D11:1"))
    )
    scores = retrieval_metrics("locomo", case, ranked)
    assert scores is not None
    assert scores["recall_any@10"] == 1.0 and scores["recall_all@10"] == 0.0


def test_longmemeval_locked_ids_and_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整清单含弃答题，写入历史和查询不含评分标注。"""
    row = {
        "question_id": "q00",
        "question_type": "multi-session",
        "question": "Where?",
        "answer": "gold-answer",
        "question_date": "2024/01/03 (Wed) 11:00",
        "haystack_session_ids": ["s2", "s1"],
        "haystack_dates": ["2024/01/02 (Tue) 11:00", "2024/01/01 (Mon) 11:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "first turn", "has_answer": True}],
            [{"role": "assistant", "content": "second turn"}],
        ],
        "answer_session_ids": ["s2"],
    }
    rows = [
        {**row, "question_id": f"q{i:03}" + ("_abs" if i >= 470 else "")}
        for i in range(500)
    ]
    name = "longmemeval_s_cleaned.json"
    digest = _put(tmp_path / name, rows)
    lock = tmp_path / "lock.json"
    _put(
        lock,
        {
            "longmemeval-s": {
                "revision": "fixture",
                "question_count": 500,
                "files": {name: digest},
            }
        },
    )
    monkeypatch.setattr(datasets, "_RELEASES", lock)
    loaded = datasets.load_dataset("longmemeval-s", tmp_path)
    assert len(loaded.cases) == 500
    assert sum(case.reference.abstention for case in loaded.cases) == 30
    first, last = loaded.select(("q499_abs", "q000"))
    assert first.query.text == "Where?"
    assert [session.id for session in loaded.histories[first.history_id]] == [
        "s1",
        "s2",
    ]
    assert (
        loaded.histories[first.history_id][0].turns[0].event_time
        == "2024-01-01T11:00:00+00:00"
    )
    assert "gold-answer" not in repr(first.query)
    assert "has_answer" not in repr(loaded.histories[first.history_id])
    retrieved = Retrieval(
        evidence=(
            Evidence(text="distractor", session_ids=("s1",)),
            Evidence(text="evidence", session_ids=("s2",)),
        ),
        provenance=True,
    )
    verdict = Completion(text="no", model="judge")
    score = score_answer(
        "longmemeval-s", first, retrieved, _answer("wrong"), judge=verdict
    )
    assert not score.correct and score.retrieval is not None
    assert score.retrieval["recall_all@5"] == 1
    assert score.retrieval["ndcg_any@5"] == 1
    assert "gold-answer" in judge_prompt("longmemeval-s", first, "wrong")
    abstention = score_answer(
        "longmemeval-s", last, retrieved, _answer("unknown"), judge=verdict
    )
    assert abstention.retrieval is None and abstention.abstention
    (tmp_path / name).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="校验和"):
        datasets.load_dataset("longmemeval-s", tmp_path)


def test_locomo_keeps_dialog_order_and_adversarial_rows(tmp_path: Path) -> None:
    """保留说话人、图注、原始对话 ID 和无答案的对抗题。"""
    path = tmp_path / "locomo10.json"
    _put(
        path,
        [
            {
                "sample_id": "dialogue",
                "conversation": {
                    "speaker_a": "A",
                    "speaker_b": "B",
                    "session_2_date_time": "3:15 am on 9 May, 2023",
                    "session_2": [{"speaker": "B", "dia_id": "D2:1", "text": "seen"}],
                    "session_1_date_time": "3:15 pm on 8 May, 2023",
                    "session_1": [
                        {
                            "speaker": "A",
                            "dia_id": "D1:1",
                            "text": "photo",
                            "blip_caption": "park",
                        }
                    ],
                },
                "qa": [
                    {
                        "question": "Where?",
                        "answer": "park",
                        "category": 4,
                        "evidence": ["D1:1; D2:1", "D1:1"],
                    },
                    {
                        "question": "Missing evidence?",
                        "answer": "park",
                        "category": 4,
                        "evidence": ["D1:1; D2:9"],
                    },
                    {"question": "Which year?", "category": 5, "evidence": []},
                ],
            }
        ],
    )
    cases, histories = datasets._load_locomo(path)
    sessions = histories["dialogue"]
    assert [session.id for session in sessions] == ["D1", "D2"]
    assert sessions[0].turns[0].text == "photo\n[Image caption: park]"
    assert sessions[1].turns[0].role == "assistant"
    assert cases[0].reference.category == "single-hop" and cases[2].reference.abstention
    assert cases[0].reference.evidence == ("D1:1", "D2:1")
    assert cases[1].reference.unmapped_evidence == ("D1:1; D2:9",)
    retrieved = Retrieval(
        evidence=(Evidence(text="park", turn_ids=("D1:1", "D2:1")),),
        provenance=True,
    )
    scores = retrieval_metrics("locomo", cases[0], retrieved)
    assert scores is not None and scores["recall_all@10"] == 1.0
    assert retrieval_metrics("locomo", cases[1], retrieved) is None


def test_personamem_public_csv_shapes(tmp_path: Path) -> None:
    """加载 Python 字面量查询、系统历史和确定性选项。"""
    file = tmp_path / "benchmark.csv"
    history = tmp_path / "history.json"
    record = {
        "chat_history_32k_link": "https://public.example/history.json",
        "user_query": "{'role': 'user', 'content': 'What is relevant?'}",
        "correct_answer": "choice gold",
        "incorrect_answers": '["one", "two", "three"]',
        "pref_type": "explicit",
    }
    with file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(record))
        writer.writeheader()
        writer.writerow(record)
    _put(
        history,
        {"chat_history": [{"role": "system", "content": "historic instruction"}]},
    )
    paths = {
        "benchmark/text/benchmark.csv": file,
        "data/chat_history_32k/history.json": history,
    }
    cases, sessions = datasets._load_persona(paths)
    case = cases[0]
    assert sessions["history.json"][0].turns[0].role == "system"
    assert cases == datasets._load_persona(paths)[0]
    choice = "ABCD"[case.query.choices.index("choice gold")]
    result = score_answer(
        "personamem-v2",
        case,
        Retrieval(evidence=(), provenance=False),
        _answer(choice),
        judge=None,
    )
    assert result.correct and result.judge is None
    with pytest.raises(ValueError, match="超出"):
        datasets.verified_files(
            tmp_path,
            datasets.Release(
                revision="fixture", question_count=1, files={"../outside": ""}
            ),
        )
