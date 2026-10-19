"""跑在 MnemoTrace harness 上的 LongMemEval 运行器（CLAUDE.md §4）。

    # 对内置样本做端到端 QA + full-context 基线（需要 LLM key）
    python eval/longmemeval.py --mode qa --limit 4

    # 大规模检索 Recall@k，无需 LLM（--data 指向 longmemeval_s.json）
    python eval/longmemeval.py --mode recall --data data/longmemeval_s.json --limit 50

两种互补的测量：
  * recall : 对每个会话 turn 做嵌入，检索 top-k，检查是否命中证据会话。不依赖 LLM，可扩展到完整的
             500 会话 haystack。测的是检索这一半。
  * qa     : ingest -> consolidate（LLM 抽取）-> 检索 facts -> LLM 作答 -> LLM 裁判，外加一个
             full-context 基线（把所有 turn 塞进 prompt），用同样的方式打分。测的是整个系统，并回答
             章程的核心问题：我们能否在准确率上击败 full-context，token 成本是多少？
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

warnings.filterwarnings("ignore")  # 屏蔽 litellm/pydantic 的序列化警告
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
os.environ.setdefault("LITELLM_LOG", "CRITICAL")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Memory  # noqa: E402
from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm, make_reranker  # noqa: E402
from mnemotrace.util import DAY, fmt_date  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = 1_700_000_000.0

ANSWER_SYSTEM = (
    "You answer the user's question using ONLY the provided context (dated FACTS and dated CONVERSATIONS). "
    "Use the dates to resolve 'current/now', 'first', 'most recent', and durations ('how long'). When facts "
    "conflict, trust the most recent one.\n"
    "If the question asks for a RECOMMENDATION or SUGGESTION, or what the user would PREFER/like: do NOT "
    "refuse. Identify the user's relevant stated preferences, habits, tools, brands, or constraints from the "
    "context, and answer with a specific suggestion that explicitly reflects them (name the preferred "
    "tool/brand/genre/style). Such questions are answerable from the user's preferences even when no literal "
    "answer is stored.\n"
    "For factual questions, answer concisely and specifically. Only when the context genuinely contains no "
    "relevant information, reply exactly: I don't know."
)
ANSWER_TEMPLATE = "Today's date: {qdate}\n\n{context}\n\nQuestion: {question}\nAnswer:"

# Phase 1 —— 带显式证据聚合步骤的 reasoning 作答。针对 _S 上 both-wrong 题目的主导失败模式：
# 计数（'how many sessions'）、求和（'how many rare items total'）、排序（'order of trips'）、日期
# 运算（'how old when X'）、最新优先（knowledge-update）。单次作答不会聚合；这里先让模型输出一段
# 简短 CoT，再输出规范的 "ANSWER: ..." 终行，供 extractor + judge 消费。
REASONING_SYSTEM = (
    "You answer the user's question using the provided dated context. The context has two parts: a FACTS "
    "index (a digest) AND the full dated CONVERSATIONS below it. The answer is USUALLY present — search "
    "BOTH parts thoroughly before concluding anything. The dates are real; use them for temporal reasoning.\n\n"
    "DIG FOR THE ANSWER FIRST. Most questions ARE answerable from the history — your job is to find the "
    "evidence, not to give up. If the FACTS digest doesn't contain it, scan the full CONVERSATIONS below.\n\n"
    "WHEN THE QUESTION NEEDS MULTIPLE EVIDENCE PIECES (counting, summing, ordering, date arithmetic, "
    "duration, earliest/latest/most-recent, multi-step lookup, knowledge updated over time):\n"
    "  EVIDENCE: list EVERY relevant dated item from the context, one per line with its date. Be EXHAUSTIVE "
    "— scan the whole history; a missed item makes a count or total wrong. Re-read before finalizing.\n"
    "  REASON: count distinct items / sum / sort by date / compute the date difference / pick the most "
    "recent. Show the work in 1-2 lines. For durations compute end_date − start_date explicitly.\n"
    "  ANSWER: <a single concise final answer — no reasoning on this line>\n\n"
    "FOR DATE/TIME/NUMBER ANSWERS: give the MOST SPECIFIC value the context supports (exact date > "
    "month+year > year; exact duration, not 'a while'). Read the exact figure from the text — don't round "
    "or approximate (e.g. 27 minutes 45 seconds, not 28 minutes).\n\n"
    "WHEN THE QUESTION ASKS FOR A RECOMMENDATION / PREFERENCE / what the user would like or how they'd want "
    "you to respond: do NOT refuse, do NOT ask a clarifying question, do NOT give generic advice. Instead, "
    "ground the answer in the user's OWN stated history: name the SPECIFIC people, places, brands, tools, "
    "past experiences or constraints they mentioned, and tailor the suggestion to those. "
    "Answer 'ANSWER: <concrete suggestion that explicitly references the user's specific stated details>'.\n\n"
    "FOR SIMPLE SINGLE-FACT QUESTIONS: go straight to 'ANSWER: <fact>'.\n\n"
    "CURRENT-STATE questions ('what is my current/latest X', 'where do I work now', anything about the "
    "present): report ONLY the most recent value by date and explicitly disregard older, superseded ones. "
    "The newest dated statement wins; earlier values are history, not the answer.\n\n"
    "When facts conflict, ALWAYS trust the most recent one by date.\n\n"
    "ABSTENTION — a careful LAST resort, only after you have searched the ENTIRE history (facts AND all "
    "conversations) and the SPECIFIC thing asked is genuinely never stated by the user. Do NOT abstain just "
    "because it wasn't in the FACTS digest or wasn't immediately obvious — the answer is usually further "
    "down in the conversations. But if the question presupposes something the user truly never mentioned "
    "(only a different/unrelated topic appears), then reply exactly 'ANSWER: I don't know' rather than "
    "guessing. Never fabricate a value to fill a gap. "
    "The line starting 'ANSWER:' is REQUIRED and must contain the final concise answer only."
)


def extract_answer(pred: str) -> str:
    """从 reasoning 回复中抽出最终的 'ANSWER: ...' 行。没有标记时回退为最后的非空行。judge 和拒答
    检查只作用于抽取出的最终答案，避免推理文本误触发拒答的子串匹配。"""
    if not pred:
        return ""
    text = pred.strip()
    # 取最后一次出现的 "ANSWER:"（大小写不敏感），捕获该行其后内容
    import re as _re

    matches = list(_re.finditer(r"(?i)\bANSWER\s*:\s*(.*)", text))
    if matches:
        tail = matches[-1].group(1).strip()
        # 若模型在后面继续写（按 prompt 不应如此），就在换行处截断
        tail = tail.split("\n")[0].strip()
        if tail:
            return tail
    # 没有 'ANSWER:' 标记。推理型骨干模型（doubao-seed、deepseek）把思维链放在单独的 reasoning_content
    # 字段里，content 直接就是最终答案 —— 且常常是多行（如个性化推荐）。只返回最后一行会把答案撕成
    # 碎片，judge 永远看不到真正答案（这曾悄悄把 preference 类别打成零分）。官方 LongMemEval judge
    # 对完整回复打分，所以无标记时返回整段文本。
    return text


# ---- L2 session map + L3 user persona（CLAUDE.md §3 分层抽象）----
# 领先的 agent-memory 系统都有这两层（腾讯 Agent-Memory 的 L2 场景块 + L3 画像，火山 OpenViking 的
# User-Memory + L0/L1/L2 分层），恰好补强我们的两个弱项类别：
#   L2 session map  -> 多会话聚合（计数/列举/对比时不漏会话）
#   L3 user persona -> preference（预构建的画像，给推荐提供依据）
# 两者都用一次 LLM 调用处理全部带日期的历史（便宜），而不是逐会话调用。

SESSION_MAP_SYSTEM = (
    "You condense a long DATED chat history into a COMPLETE session-by-session map, for questions that "
    "aggregate across sessions (counting, listing, comparing, 'how many', 'which ones'). For EVERY dated "
    "conversation, emit one line: '[date] key facts/events/items/decisions/topics in it'. Be COMPLETE and "
    "specific — include every distinct thing the user mentions (names, places, purchases, trips, activities, "
    "numbers), because a later question may need to count or list them. Do NOT merge or skip conversations; "
    "preserve chronological order."
)

PERSONA_SYSTEM = (
    "You build a concise USER PROFILE from a long chat history, to help answer what the user prefers or "
    "would want. Capture WITH SPECIFICS: stated preferences and dislikes; habits and routines; owned "
    "tools/brands/devices; hobbies and interests; skill levels; goals; constraints. Quote concrete details "
    "(names, brands, places, genres). Group into short labeled lines. Include ONLY what the user actually "
    "stated — do not invent."
)


def build_session_map(llm, history_text: str) -> str:
    """L2：一次密集调用，把完整的带日期历史转成按时间序的完整会话摘要。"""
    try:
        out = llm.complete(f"{history_text}\n\nComplete session-by-session map:", system=SESSION_MAP_SYSTEM)
        return out.strip()
    except Exception:  # noqa: BLE001
        return ""


def build_persona(llm, history_text: str) -> str:
    """L3：一次调用，把用户的偏好/习惯/所有物综合成一份紧凑画像。"""
    try:
        out = llm.complete(f"{history_text}\n\nUSER PROFILE:", system=PERSONA_SYSTEM)
        return out.strip()
    except Exception:  # noqa: BLE001
        return ""


# ---- 针对两个弱项类别的作答策略（CLAUDE.md Bet B）----
# 按问题文本路由，而非 benchmark 的类别标签，这样逻辑能泛化到真实使用场景（部署系统看不到
# 'question_type'），也不是在钻评测空子。

_NUM_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
_COUNT_CUES = ("how many", "how much", "how often", "number of", "total number", "in total",
               "altogether", "combined", "count of", "how long", "how old", "list all", "all the")
_PREF_CUES = ("recommend", "suggest", "what should i", "what would i", "would i prefer", "help me",
              "advice", "i'm planning", "im planning", "i am planning", "i'm thinking", "what kind of",
              "what type of", "which one should", "any ideas", "what do you suggest")


def needs_self_consistency(question: str) -> bool:
    """计数/求和/时长/聚合类问题：单次作答会随机数错（_S 上观察到 5->4、4->6）。采样 K 个答案取
    众数，可以抵消这种随机误差。"""
    q = question.lower()
    return any(c in q for c in _COUNT_CUES)


def needs_two_stage_pref(question: str) -> bool:
    """推荐/偏好类问题：模型默认输出泛泛建议。强制它先列出用户明确说过的具体历史、再据此作答，
    弥补个性化短板。"""
    q = question.lower()
    return any(c in q for c in _PREF_CUES)


def _norm_vote(ans: str) -> str:
    """为多数投票归一化答案：转小写、去掉冠词/填充词、数字词映射为数字、压缩空白。这样
    'four model kits' 和 'I worked on 4 kits' 都投给 '4'。"""
    import re as _re
    s = ans.lower().strip().strip(".!?\"' ")
    s = _re.sub(r"\b(the|a|an|i|you|your|my|have|has|had|worked|on|or|bought|in|of|total|there|are|is|were|was)\b", " ", s)
    toks = [_NUM_WORDS.get(t, t) for t in _re.split(r"\s+", s) if t]
    # 若出现数字，就只对数字投票 —— 它是计数/时长题的决定性内容
    nums = [t for t in toks if _re.fullmatch(r"\d+(\.\d+)?", t)]
    if nums:
        return nums[0]
    return " ".join(toks)


def answer_self_consistency(answerer, prompt: str, system: str, k: int = 5) -> str:
    """带温度采样 k 个答案，返回归一化后出现最多的那个原始答案。返回完整文本（而非归一化 key），
    让官方 judge 看到真实的答案。"""
    from collections import Counter
    cands = []
    for i in range(k):
        raw = answerer.complete(prompt, system=system, temperature=0.0 if i == 0 else 0.7)
        cands.append(extract_answer(raw))
    cands = [c for c in cands if c]
    if not cands:
        return ""
    votes = Counter(_norm_vote(c) for c in cands)
    winner_key, _ = votes.most_common(1)[0]
    # 返回映射到获胜 key 的完整答案中最长的一个（携带最多上下文）
    matching = [c for c in cands if _norm_vote(c) == winner_key]
    return max(matching, key=len)


PREF_EXTRACT_SYSTEM = (
    "You read a user's long chat history and surface ONLY what they themselves explicitly stated that is "
    "relevant to the question — their concrete preferences, habits, owned tools/brands, past experiences, "
    "skill level, and constraints. Quote specifics (names, brands, places, dates). 3-6 bullet points. "
    "If they stated nothing relevant, say 'NONE'. Do not invent or generalize."
)


def answer_two_stage_pref(answerer, context: str, question: str, qdate: str, answer_system: str) -> str:
    """阶段 1：拉出用户明确说过的相关历史。阶段 2：作答，把这段历史钉在 context 顶部，迫使模型
    据此作答，而不是给出泛泛建议。"""
    relevant = answerer.complete(
        f"{context}\n\nQuestion: {question}\n\nThe user's relevant stated details:",
        system=PREF_EXTRACT_SYSTEM, temperature=0.0,
    ).strip()
    pinned = f"THE USER'S RELEVANT STATED HISTORY (ground your answer in these specifics):\n{relevant}\n\n{context}"
    return answerer.complete(
        ANSWER_TEMPLATE.format(qdate=qdate, context=pinned, question=question),
        system=answer_system, temperature=0.0,
    )


# LongMemEval_S 的 haystack 平均约 497k 字符（≈124k token）—— 远在 gemini-2.5-flash/pro 的 1M 窗口之内。
# 600k 留有余量且不截断任何 item；旧的 200k 只覆盖了前 40%（最老的会话！）。
FC_CHAR_BUDGET = 600_000

JUDGE_SYSTEM = "You grade answers. Reply with only 'yes' or 'no'."
# 通用回退（仅用于官方集合之外的类别）。优先用 official_judge_prompt()。
JUDGE_TEMPLATE = (
    "Question: {q}\nReference answer: {gold}\nCandidate answer: {pred}\n\n"
    "Does the candidate convey the same key information as the reference? Reply yes or no."
)


def official_judge_prompt(task: str, question: str, answer: str, response: str, abstention: bool = False) -> str:
    """LongMemEval 官方 judge prompt 的逐字版本（external/LongMemEval/.../evaluate_qa.py）。

    逐字匹配官方 prompt，我们的数字才能与已发布的榜单直接可比（OMEGA 95.4、Mem0 94.4、Hunyuan 85.2）。
    这些 prompt 刻意按类别区分，且比朴素的"信息一致吗"judge 更宽松 —— temporal 容忍差一天；
    knowledge-update 接受同时复述旧信息的答案；preference 是 rubric 制，不必覆盖每一条。用更严格的
    judge 会压低我们的分数并失去可比性（CLAUDE.md Bet D：做可信的记分板 —— 意味着按 benchmark 定义
    的方式打分，而不是更严）。"""
    if abstention:
        return ("I will give you an unanswerable question, an explanation, and a response from a model. "
                "Please answer yes if the model correctly identifies the question as unanswerable. The model "
                "could say that the information is incomplete, or some other information is given but the asked "
                "information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model "
                "correctly identify the question as unanswerable? Answer yes or no only.").format(question, answer, response)
    if task in ("single-session-user", "single-session-assistant", "multi-session"):
        return ("I will give you a question, a correct answer, and a response from a model. Please answer yes "
                "if the response contains the correct answer. Otherwise, answer no. If the response is equivalent "
                "to the correct answer or contains all the intermediate steps to get the correct answer, you should "
                "also answer yes. If the response only contains a subset of the information required by the answer, "
                "answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response "
                "correct? Answer yes or no only.").format(question, answer, response)
    if task == "temporal-reasoning":
        return ("I will give you a question, a correct answer, and a response from a model. Please answer yes if "
                "the response contains the correct answer. Otherwise, answer no. If the response is equivalent to "
                "the correct answer or contains all the intermediate steps to get the correct answer, you should "
                "also answer yes. If the response only contains a subset of the information required by the answer, "
                "answer no. In addition, do not penalize off-by-one errors for the number of days. If the question "
                "asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
                "predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}"
                "\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no "
                "only.").format(question, answer, response)
    if task == "knowledge-update":
        return ("I will give you a question, a correct answer, and a response from a model. Please answer yes if "
                "the response contains the correct answer. Otherwise, answer no. If the response contains some "
                "previous information along with an updated answer, the response should be considered as correct as "
                "long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel "
                "Response: {}\n\nIs the model response correct? Answer yes or no only.").format(question, answer, response)
    if task == "single-session-preference":
        return ("I will give you a question, a rubric for desired personalized response, and a response from a "
                "model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The "
                "model does not need to reflect all the points in the rubric. The response is correct as long as it "
                "recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\n"
                "Model Response: {}\n\nIs the model response correct? Answer yes or no only.").format(question, answer, response)
    # 未知类别 -> 通用模板
    return JUDGE_TEMPLATE.format(q=question, gold=answer, pred=response)

_ABSTAIN_MARKERS = (
    "don't know", "do not know", "not mentioned", "no information", "cannot find",
    "not sure", "unknown", "not stated", "doesn't mention", "isn't mentioned", "no answer",
)


def is_abstention(item: dict) -> bool:
    return str(item.get("question_id", "")).endswith("_abs") or not str(item.get("answer", "")).strip()


def looks_like_abstention(pred: str) -> bool:
    p = pred.lower()
    return any(m in p for m in _ABSTAIN_MARKERS)


def load_data(path: str | None) -> list[dict]:
    if path is None:
        path = os.path.join(HERE, "longmemeval_sample.json")
    elif path in ("oracle", "s", "m", "longmemeval_oracle", "longmemeval_s", "longmemeval_m"):
        from huggingface_hub import hf_hub_download

        filename = path if path.startswith("longmemeval_") else f"longmemeval_{path}"
        path = hf_hub_download(repo_id="xiaowu0162/longmemeval", filename=filename, repo_type="dataset")
    elif path in ("locomo", "locomo10"):
        # 同一 harness 上的第二个 benchmark：把 LOCOMO 转成 LongMemEval item（见 eval/locomo.py）。
        from eval.locomo import load_locomo

        return load_locomo()
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sessions_of(item: dict):
    sessions = item.get("haystack_sessions", [])
    ids = item.get("haystack_session_ids") or [f"sess_{i}" for i in range(len(sessions))]
    return list(zip(ids, sessions))


def parse_date(s):
    """解析形如 '2023/05/20 (Sat) 02:21' 的 LongMemEval 日期 -> (epoch, 'YYYY-MM-DD')。真实日期对
    时序推理问题（'first'、'how long'、'most recent'）至关重要。"""
    if not s:
        return None
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    tm = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    try:
        ts = datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp()
        return ts, f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def ingest(mem: Memory, item: dict, user_id: str) -> None:
    # 每个会话一个 episode（拼接各 turn）：抽取调用比按 turn 少约 10 倍，且 episode->session 映射
    # 一一对应，便于 recall。会话是一个连贯的记忆单元。
    dates = item.get("haystack_dates") or []
    texts: list[str] = []
    meta: list[tuple] = []
    for idx, (sid, turns) in enumerate(sessions_of(item)):
        parsed = parse_date(dates[idx] if idx < len(dates) else None)
        event_time, dstr = parsed if parsed else (BASE + idx * DAY, None)
        text = "\n".join(
            f"{t.get('role', 'user')}: {t.get('content', '')}" for t in turns if t.get("content")
        )
        if text.strip():
            texts.append(text)
            meta.append((sid, event_time, dstr))
    if not texts:
        return
    # 用一次 model.encode 批量嵌入整个 haystack（否则逐会话循环会在 GPU 锁上串行化约 50 次嵌入，
    # 在 _S 规模下主导墙钟时间）。
    vecs = mem.embedder.embed_batch(texts)
    for (sid, event_time, dstr), text, vec in zip(meta, texts, vecs):
        ep = mem.add(text, user_id=user_id, session_id=sid, speaker="session",
                     event_time=event_time, embedding=vec)
        ep.metadata["date"] = dstr or fmt_date(event_time)


def all_text(item: dict, reverse: bool = False) -> str:
    """把所有会话序列化为文本。reverse=True 时最新会话在前 —— 更适合 knowledge-update 问题（LLM 应
    信任最新 fact）。按时间序（默认）更适合时序排序类问题。REASONING_SYSTEM prompt 靠日期标注两者
    兼顾。"""
    dates = item.get("haystack_dates") or []
    sessions_list = list(sessions_of(item))
    dates_list = list(dates) + [None] * max(0, len(sessions_list) - len(dates))
    combined = list(zip(sessions_list, dates_list))
    if reverse:
        combined = list(reversed(combined))
    out = []
    for (sid, turns), date_str in combined:
        parsed = parse_date(date_str)
        out.append(f"=== Conversation on {parsed[1] if parsed else f'session {sid}'} ===")
        for turn in turns:
            out.append(f"[{turn.get('role','user')}] {turn.get('content','')}")
    return "\n".join(out)


def judge_correct(item: dict, pred: str, judge_llm) -> bool:
    """用官方 LongMemEval 按类别区分的 judge 给预测打分（保证我们的数字与榜单可比）。所有预测都送给
    judge —— 不做预过滤 —— 与官方 evaluate_qa.py 完全一致。（早先的 'looks_like_abstention' 短路逻辑
    会压低 temporal 分数：它凭子串 'not sure' 拒掉了 'June 2023 (not 100% sure)' 这样的答案，尽管
    June 2023 是对的。纯 'I don't know' judge 自己就会判错。）"""
    abstain = is_abstention(item)
    task = item.get("question_type", "?")
    prompt = official_judge_prompt(task, item["question"], item.get("answer", ""), pred, abstention=abstain)
    verdict = judge_llm.complete(prompt, system=JUDGE_SYSTEM).strip().lower()
    return verdict.startswith("y") or verdict[:5].find("yes") != -1


def run_recall(items, embedder, top_k):
    hit, recall, n = 0, 0.0, 0
    for item in items:
        evidence = set(item.get("answer_session_ids") or [])
        if not evidence:
            continue
        mem = Memory(embedder=embedder)  # 不传 llm：纯检索
        ingest(mem, item, item["question_id"])
        qvec = embedder.embed(item["question"])
        results = mem.episodes_vec.search(qvec, top_k)
        retrieved_sessions = {ep.session_id for _, ep in results}
        got = retrieved_sessions & evidence
        hit += 1 if got else 0
        recall += len(got) / len(evidence)
        n += 1
    return {"hit@k": hit / n if n else 0.0, "recall@k": recall / n if n else 0.0, "n": n}


def eval_one(item, embedder, extractor_llm, answerer_llm, judge_llm, top_k, k_chunks, extract_k=0, reranker=None):
    """评测单个问题。返回结果 dict；绝不抛异常（错误会被捕获）。"""
    qid, cat, q = item["question_id"], item.get("question_type", "?"), item["question"]
    try:
        mem = Memory(embedder=embedder, llm=extractor_llm, reranker=reranker)
        ingest(mem, item, qid)
        if extract_k and extract_k > 0:
            # _S 规模：先检索再抽取。对全部约 500 个会话抽取 fact 不可行，所以针对当前问题检索
            # （bi-encoder + reranker）top extract_k 个会话，只整合这些。无论 haystack 多大，都能
            # 封顶 LLM 抽取成本。
            top_eps = mem.retrieve_episodes(q, qid, extract_k)
            mem.engine.consolidate(top_eps)
        else:
            mem.consolidate()

        qdate = item.get("question_date", "")
        t0 = time.perf_counter()
        # 通过核心 API 拿到混合的、带日期戳的 context：存活 fact（已消解冲突/当前有效）+ top-k
        # 原始 chunk（细节）。日期让 answerer 能解出时序与知识更新类问题。
        context = mem.context_for(q, user_id=qid, top_k=top_k, k_chunks=k_chunks)
        pred = answerer_llm.complete(ANSWER_TEMPLATE.format(qdate=qdate, context=context, question=q), system=ANSWER_SYSTEM)
        latency = (time.perf_counter() - t0) * 1000.0
        eng_ok = judge_correct(item, pred, judge_llm)

        # full-context 基线 —— 单独隔离，它的失败（如 _S 上下文超出窗口）不能拖垮 engram 的测量。
        # 上限截到模型窗口（full-context 本来就无法扩展；这正是要证明的点）。
        fc_ok, fc_tok = None, 0
        try:
            fc_context = all_text(item)[:FC_CHAR_BUDGET]
            fc_pred = answerer_llm.complete(ANSWER_TEMPLATE.format(qdate=qdate, context=fc_context, question=q), system=ANSWER_SYSTEM)
            fc_ok = judge_correct(item, fc_pred, judge_llm)
            fc_tok = len(fc_context.split())
        except Exception:  # noqa: BLE001
            fc_ok, fc_tok = None, 0
        return {"qid": qid, "cat": cat, "eng": eng_ok, "fc": fc_ok, "eng_tok": len(context.split()),
                "fc_tok": fc_tok, "lat": latency, "pred": pred.strip()[:60], "err": None}
    except Exception as e:  # noqa: BLE001 - 单个坏 item 不能杀死 500 题的运行
        return {"qid": qid, "cat": cat, "eng": None, "fc": None, "err": f"{type(e).__name__}: {str(e)[:90]}"}


def run_qa(items, embedder, extractor_llm, answerer_llm, judge_llm, top_k, k_chunks, workers=1, out=None, extract_k=0, reranker=None):
    eng_hits, fc_hits = defaultdict(list), defaultdict(list)
    eng_tokens, fc_tokens, latencies = [], [], []
    errors = 0
    lock = threading.Lock()
    outfh = None
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        outfh = open(out, "w", encoding="utf-8")
    state = {"done": 0}

    def handle(r):
        with lock:
            state["done"] += 1
            n = state["done"]
            if r["err"]:
                nonlocal errors
                errors += 1
                print(f"  [{n}/{len(items)}] {r['qid']:14} ERROR {r['err']}", flush=True)
            else:
                eng_hits[r["cat"]].append(r["eng"])
                eng_tokens.append(r["eng_tok"])
                latencies.append(r["lat"])
                if r["fc"] is not None:  # _S 上 full-context 可能缺失（塞不下）
                    fc_hits[r["cat"]].append(r["fc"])
                    fc_tokens.append(r["fc_tok"])
                if n <= 5 or n % 25 == 0:
                    e = 100.0 * sum(v for vs in eng_hits.values() for v in vs) / max(1, sum(len(v) for v in eng_hits.values()))
                    print(f"  [{n}/{len(items)}] running MnemoTrace acc={e:.1f}%  (last {r['cat']}: "
                          f"eng={'Y' if r['eng'] else 'n'} fc={'Y' if r['fc'] else 'n'})", flush=True)
            if outfh:
                outfh.write(json.dumps(r, ensure_ascii=False) + "\n")
                outfh.flush()

    args = (embedder, extractor_llm, answerer_llm, judge_llm, top_k, k_chunks, extract_k, reranker)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(eval_one, it, *args) for it in items]
            for fut in as_completed(futures):
                handle(fut.result())
    else:
        for it in items:
            handle(eval_one(it, *args))
    if outfh:
        outfh.close()
    return eng_hits, fc_hits, eng_tokens, fc_tokens, latencies, errors


def _acc(hits):
    flat = [h for v in hits.values() for h in v]
    return 100.0 * sum(flat) / len(flat) if flat else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None, help="path to longmemeval json (default: bundled sample)")
    ap.add_argument("--mode", choices=["recall", "qa", "both"], default="qa")
    ap.add_argument("--limit", type=int, default=0, help="cap number of items (0 = all)")
    ap.add_argument("--embedder", default="bge-small")
    ap.add_argument("--extractor", default="deepseek")
    ap.add_argument("--answerer", default="deepseek")
    ap.add_argument("--judge", default="qwen-plus")
    ap.add_argument("--topk", type=int, default=10, help="facts retrieved into context")
    ap.add_argument("--chunks", type=int, default=3, help="raw session chunks retrieved into context (0=facts only)")
    ap.add_argument("--workers", type=int, default=1, help="parallel questions (use 6-8 for the full set)")
    ap.add_argument("--out", default=None, help="write per-question results JSONL here (checkpoint)")
    ap.add_argument("--extract-k", type=int, default=0, dest="extract_k",
                    help="_S scale: extract facts only from the top-N retrieved sessions (0=consolidate all)")
    ap.add_argument("--reranker", default="none", help="none | bge-reranker | bge-reranker-large | bge-reranker-v2")
    args = ap.parse_args()

    load_dotenv()
    items = load_data(args.data)
    if args.limit and args.limit < len(items):
        # 等距抽样，让小切片也覆盖所有题型（文件按题型排序）
        stride = max(1, len(items) // args.limit)
        items = items[::stride][: args.limit]
    print(f"LongMemEval runner: {len(items)} items, mode={args.mode}, embedder={args.embedder}, top_k={args.topk}")
    embedder = make_embedder(args.embedder)

    if args.mode in ("recall", "both"):
        r = run_recall(items, embedder, args.topk)
        print(f"\n== RECALL (LLM-free, n={r['n']}) ==")
        print(f"   hit@{args.topk}    {100*r['hit@k']:.1f}%")
        print(f"   recall@{args.topk} {100*r['recall@k']:.1f}%")

    if args.mode in ("qa", "both"):
        print(f"\n== QA (extractor={args.extractor}, answerer={args.answerer}, judge={args.judge}, "
              f"reranker={args.reranker}, extract_k={args.extract_k}, workers={args.workers}) ==")
        ex_llm = make_llm(args.extractor, max_tokens=512, num_retries=3, timeout=60)
        ans_llm = make_llm(args.answerer, max_tokens=128, num_retries=3, timeout=60)
        jdg_llm = make_llm(args.judge, max_tokens=8, num_retries=3, timeout=60)
        reranker = make_reranker(args.reranker)
        eng_hits, fc_hits, eng_tok, fc_tok, lat, errors = run_qa(
            items, embedder, ex_llm, ans_llm, jdg_llm, args.topk, args.chunks, args.workers, args.out,
            args.extract_k, reranker
        )
        if errors:
            print(f"\n   (note: {errors} item(s) errored and were excluded)")
        cats = sorted(set(eng_hits) | set(fc_hits))
        print("\n   per-category accuracy (MnemoTrace / full-context):")
        for c in cats:
            e = 100.0 * sum(eng_hits[c]) / len(eng_hits[c]) if eng_hits[c] else 0.0
            f = 100.0 * sum(fc_hits[c]) / len(fc_hits[c]) if fc_hits[c] else 0.0
            print(f"     {c:24} {e:6.1f}% / {f:6.1f}%")
        avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
        print("\n   OVERALL")
        print(f"     accuracy        MnemoTrace {_acc(eng_hits):5.1f}%   full-context {_acc(fc_hits):5.1f}%")
        print(f"     context tokens  MnemoTrace {avg(eng_tok):6.1f}   full-context {avg(fc_tok):7.1f}   "
              f"({avg(fc_tok)/max(1,avg(eng_tok)):.1f}x leaner)")
        print(f"     p50 latency ms  {_percentile(lat, 50):.0f} (retrieve+answer)")
        print(f"     p95 latency ms  {_percentile(lat, 95):.0f} (retrieve+answer)")


if __name__ == "__main__":
    main()
