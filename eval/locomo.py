"""LOCOMO -> LongMemEval item 适配器（AGENTS.md Bet D：同一 harness 上的第二个 benchmark）。

把公开的 LOCOMO 数据集（Maharana et al., ACL 2024; snap-research/locomo，`data/locomo10.json`）转换成
`eval/bench.py` 消费的 item 形状，这样 `python eval/bench.py --data locomo ...` 就能让每个系统走与
LongMemEval 完全相同的 answerer + judge。每个 LOCOMO QA 对生成一个 LongMemEval item；该 item 的
"haystack" 是样本完整的多会话对话。

获取数据：从 https://github.com/snap-research/locomo 下载 `locomo10.json`（`data/locomo10.json`），
放到 `eval/locomo10.json`（或给 `load_locomo` 传显式路径）。

自测（无需数据集、无需 API key）：  python -m eval.locomo
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATHS = [os.path.join(HERE, "locomo10.json"), os.path.join(HERE, "data", "locomo10.json")]

# LOCOMO QA 类别 id -> 可读的 question_type。类别 5 是 adversarial（答案不在对话中），映射到
# LongMemEval 的拒答处理：金标准答案为空 + qid 加 "_abs" 后缀。
CATEGORY = {1: "multi-hop", 2: "temporal-reasoning", 3: "open-domain", 4: "single-hop", 5: "adversarial"}
ADVERSARIAL_CAT = 5

# LOCOMO 的时间戳形如 "1:56 pm on 8 May, 2023"。归一化为 eval.longmemeval.parse_date 能识别的
# "YYYY/MM/DD HH:MM"，让时序推理问题拿到真实日期（而非合成日期）。
_DATE_FORMATS = ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %B %Y", "%H:%M on %d %B, %Y")


def _norm_date(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y/%m/%d %H:%M")
        except ValueError:
            continue
    return s  # 原样返回；parse_date 会回退到合成的递增日期


def _turn_text(turn: dict) -> str:
    txt = (turn.get("text") or "").strip()
    cap = (turn.get("blip_caption") or "").strip()  # LOCOMO 是多模态的；把图片描述并入文本
    if cap:
        txt = (txt + f" (shares a photo: {cap})").strip()
    return txt


def _sessions(conv: dict):
    """按会话时间顺序（session_1, session_2, ...）依次产出 (session_key, date_str, turns)。"""
    nums = sorted(int(m.group(1)) for k in conv for m in [re.fullmatch(r"session_(\d+)", k)] if m)
    for i in nums:
        yield f"session_{i}", conv.get(f"session_{i}_date_time", ""), conv.get(f"session_{i}", [])


def convert_sample(sample: dict) -> list[dict]:
    """一个 LOCOMO 样本（一段对话 + 其 QA 列表）-> 一组 LongMemEval 形状的 item。"""
    conv = sample.get("conversation", {})
    sample_id = sample.get("sample_id") or sample.get("id") or "locomo"

    session_ids: list[str] = []
    sessions: list[list[dict]] = []
    dates: list[str] = []
    for skey, date, turns in _sessions(conv):
        msgs = [{"role": t.get("speaker", "user"), "content": _turn_text(t)} for t in turns if _turn_text(t)]
        if not msgs:
            continue
        session_ids.append(skey)
        sessions.append(msgs)
        dates.append(_norm_date(date))
    qdate = dates[-1] if dates else ""  # 问题视为在最近的会话时点"提出"

    items: list[dict] = []
    for j, qa in enumerate(sample.get("qa", [])):
        cat = qa.get("category")
        adversarial = cat == ADVERSARIAL_CAT
        answer = "" if adversarial else str(qa.get("answer", qa.get("adversarial_answer", "")))
        qid = f"{sample_id}_q{j}" + ("_abs" if adversarial else "")
        items.append({
            "question_id": qid,
            "question": qa.get("question", ""),
            "answer": answer,
            "question_type": CATEGORY.get(cat, str(cat)),
            "question_date": qdate,
            "haystack_session_ids": session_ids,
            "haystack_sessions": sessions,
            "haystack_dates": dates,
        })
    return items


def load_locomo(path: str | None = None) -> list[dict]:
    """加载 locomo10.json，把每个样本的 QA 转成 LongMemEval item。"""
    if path is None:
        path = next((p for p in DEFAULT_PATHS if os.path.exists(p)), None)
    if not path or not os.path.exists(path):
        raise FileNotFoundError(
            "LOCOMO data not found. Download locomo10.json from https://github.com/snap-research/locomo "
            f"(data/locomo10.json) and place it at {DEFAULT_PATHS[0]}, or pass an explicit path."
        )
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    items: list[dict] = []
    for sample in raw:
        items.extend(convert_sample(sample))
    return items


def _selftest() -> None:
    """用合成的 LOCOMO 形状样本离线校验转换逻辑（无需数据集/key）。"""
    sample = {
        "sample_id": "convX",
        "conversation": {
            "speaker_a": "Ann", "speaker_b": "Bob",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Ann", "dia_id": "D1:1", "text": "I adopted a dog named Rex."},
                {"speaker": "Bob", "dia_id": "D1:2", "text": "Nice!", "blip_caption": "a brown dog"},
            ],
            "session_2_date_time": "2:30 pm on 15 June, 2023",
            "session_2": [
                {"speaker": "Ann", "dia_id": "D2:1", "text": "Rex learned to sit."},
            ],
        },
        "qa": [
            {"question": "What is Ann's dog's name?", "answer": "Rex", "category": 4, "evidence": ["D1:1"]},
            {"question": "When did Ann adopt Rex?", "answer": "8 May 2023", "category": 2},
            {"question": "What car does Ann drive?", "adversarial_answer": "Not mentioned", "category": 5},
        ],
    }
    items = convert_sample(sample)
    assert len(items) == 3, len(items)
    a, t, adv = items
    # 单跳 item
    assert a["question_id"] == "convX_q0" and a["question_type"] == "single-hop" and a["answer"] == "Rex"
    assert a["haystack_session_ids"] == ["session_1", "session_2"]
    assert a["haystack_sessions"][0][0] == {"role": "Ann", "content": "I adopted a dog named Rex."}
    assert "brown dog" in a["haystack_sessions"][0][1]["content"]            # blip 描述已并入
    assert a["haystack_dates"][0] == "2023/05/08 13:56"                       # 日期已归一化
    assert a["question_date"] == "2023/06/15 14:30"                          # 最近会话的日期
    # 时序 item
    assert t["question_type"] == "temporal-reasoning"
    # adversarial -> 拒答
    assert adv["question_id"].endswith("_abs") and adv["answer"] == "" and adv["question_type"] == "adversarial"
    # 与 harness 自带辅助函数的兼容性
    from eval.longmemeval import is_abstention, parse_date, sessions_of
    assert parse_date(a["haystack_dates"][0])[1] == "2023-05-08"
    assert is_abstention(adv) is True and is_abstention(a) is False
    assert len(sessions_of(a)) == 2
    print("eval/locomo.py self-test OK: 1 sample ->", len(items), "items; dates + abstention + shape verified")


if __name__ == "__main__":
    _selftest()
