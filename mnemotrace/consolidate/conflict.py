"""廉价、无 LLM 的冲突检测（AGENTS.md Bet C）。

当新 fact 更新了某个实体的单值属性时，它取代旧值。解决方式是非破坏性的：我们设置旧 fact 的
`invalid_at`（世界时间）和 `expired_at`（信念时间），并令 `new.supersedes -> old`，保留历史，
as-of 查询因此仍然可用。

两个检测器，都不依赖 LLM（只有真正模糊的情况才升级到 LLM——这里不需要）：

1. EXACT-SLOT —— 新旧 fact 共享 `(user, subject, predicate)` 但 object 不同。确定性；离线
   RuleExtractor 的规范 predicate（works_at、lives_in 等）都走这条路。

2. SEMANTIC（需要真实 embedder）—— 新旧 fact 共享 *subject* 且嵌入相近（同一属性），但 LLM 提取器
   给了它们 *不同的自由格式 predicate*。这是常见且此前被漏掉的情况："attends_yoga twice a week"（8 月）
   与 "does_yoga three times a week"（11 月）从不共享槽位，于是两者都存活，这对矛盾的 fact 在
   知识更新类问题上会干扰回答者。对渲染后的 fact 文本算嵌入相似度，无需调用 LLM 即可抓住它。

单值是默认：predicate 默认视为"只有一个当前值"，除非它出现在 `MULTI_VALUED` 中（真正会累积的关系
——likes、owns、visited 等）。翻转这个默认（原来是 3 项白名单）正是让知识更新能够真正失效化的关键。
语义路径由相似度阈值 + 同样的多值守卫双重约束，所以累积型偏好（"likes pizza" / "likes pasta"）
永远不会被合并。
"""
from __future__ import annotations

import re
from typing import Optional

from ..embed import Embedder
from ..types import Fact
from ..util import cosine

# 包含关系（"Contained"）检测用的实义词——忽略短词/虚词，这样才能识别
# "Charles is my boss" ⊂ "Charles is my boss and a branch manager"。
_STOP = frozenset({"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "is", "are", "was",
                   "were", "my", "his", "her", "their", "with", "for", "as", "by"})


def _content_tokens(s: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in _STOP)

# 值会累积的 predicate——一个人合法地同时拥有多个。它们从不取代；其余 predicate 都被视为
# "只有一个当前值"的属性（状态），会被更新的 fact 取代。
# 注意（依据诊断数据调优，不是拍脑袋）：目标、愿望、在做的事以及离散事件
# （booked/ordered/accepted/inherited/made ...）都是多值——它们是日志，不是可变槽位。
# 若把它们当单值，语义路径会在后来的 `goal save_money` 到达时错误地废弃 `goal reduce_sugar_intake`。
# 不要在这里加入裸的 "does"/"attends"——那会重新破坏瑜伽频率更新（predicate "does_yoga"/"attends_yoga"）。
MULTI_VALUED = {
    "likes", "dislikes", "prefers", "enjoys", "loves", "hates", "avoids", "wants", "interested_in",
    "owns", "has", "bought", "purchased", "visited", "traveled_to", "went_to", "been_to",
    "knows", "met", "friends_with", "read", "watched", "played", "listened_to", "tried", "ate",
    "speaks", "allergic_to", "has_pet", "has_child", "plans", "plans_to", "recommends", "mentioned",
    "uses", "needs", "considering",
    # 关系/亲属（一个人可以有多个兄弟姐妹、朋友、同事、子女等）
    "colleague", "coworker", "co_worker", "friend", "sister", "brother", "mother", "father",
    "parent", "child", "spouse", "wife", "husband", "partner",
    # 目标/愿望（可以同时持有多个）
    "goal", "goals", "hopes", "wishes", "aspires", "working_on", "works_on", "work_on",
    # 项目/agent 记忆笔记（一个 repo 可以有多条持久规则与决策）
    "rule", "preference", "decision", "note",
    "project_rule", "project_preference", "project_decision", "project_note", "agent_instruction",
    # 离散事件/活动日志（每次都是独立发生，不是可被替换的状态）
    "made", "makes", "ordered", "booked", "accepted", "inherited", "received", "sent", "gave",
    "asked", "discussed", "did", "attended", "used_service", "celebrated", "experienced",
}


def is_single_valued(predicate: str) -> bool:
    """单值 = 当前状态属性（同一时刻只有一个值）。默认 True；只有 MULTI_VALUED 中明确
    会累积的 predicate 是多值。`favorite_<x>` 保持单值（每个 x 只有一个最爱）。"""
    p = predicate.lower()
    if p in MULTI_VALUED:
        return False
    # "likes_<x>" / "owns_<x>" 这类复合 predicate 也会累积
    return not any(p.startswith(mv + "_") for mv in MULTI_VALUED)


def _norm(s: str) -> str:
    return s.strip().lower()


# 偏好极性——用于检测同一 object 上的 喜欢↔讨厌 反转。
_POS_PREF = {"likes", "like", "loves", "love", "enjoys", "enjoy", "prefers", "prefer", "favorite",
             "favourite", "fond_of", "into", "interested_in", "fan_of"}
_NEG_PREF = {"dislikes", "dislike", "hates", "hate", "avoids", "avoid", "not_into", "disinterested_in"}


def _pref_polarity(pred: str) -> Optional[str]:
    p = pred.lower()
    if p in _POS_PREF or p.startswith(("favorite", "favourite", "likes_", "loves_", "enjoys_", "prefers_")):
        return "like"
    if p in _NEG_PREF or p.startswith(("dislikes_", "dislike_", "hates_", "avoids_", "doesn't_", "does_not_")):
        return "dislike"
    return None


def _protected(old: Fact, new: Fact) -> bool:
    """用户亲自断言的 fact 绝不会被自动提取的 fact 自动取代。"""
    return old.source == "user" and new.source != "user"


def _supersede(old: Fact, new: Fact) -> None:
    """非破坏性失效化：old 从 new.valid_at 起在世界时间上不再成立，从 new.created_at 起不再被
    相信；new 记录它取代了谁。历史被保留，供 as-of 查询使用。"""
    if old.invalid_at is None:
        old.invalid_at = new.valid_at
    old.expired_at = new.created_at
    new.supersedes = old.id


_ADJUDICATE_SYSTEM = (
    "You decide whether a NEW statement about a user contradicts (replaces) an OLD one, or coexists with "
    "it. Reply with exactly one word: REPLACES if the new fact updates/overrides the old (same attribute, "
    "new value), or COEXISTS if both can be true at once. Nothing else."
)


class ConflictResolver:
    def __init__(self, embedder: Optional[Embedder] = None, sim_threshold: float = 0.80,
                 llm=None, ambiguous_band: float = 0.12) -> None:
        # embedder 只用于语义路径；为 None 时（离线/hashing）只有 exact-slot 生效，
        # 保证零依赖 demo 和测试完全确定。
        self.embedder = embedder
        self.sim_threshold = sim_threshold
        # 可选的 LLM 裁决器（AGENTS.md 「IV. 不允许静默损坏记忆」的歧义裁决）：对刚好低于自动取代阈值的模糊区间，询问 LLM
        # 新 fact 是否取代旧 fact。未传入 llm 时关闭——常见路径保持无 LLM（Bet C），
        # 只有真正边界的情况才升级。
        self.llm = llm
        self.ambiguous_band = ambiguous_band

    def _llm_says_replaces(self, new: Fact, old: Fact) -> bool:
        if self.llm is None:
            return False
        try:
            verdict = self.llm.complete(
                f"OLD: {old.text}\nNEW: {new.text}\n\nDoes NEW replace OLD?", system=_ADJUDICATE_SYSTEM)
            return "replace" in verdict.strip().lower()
        except Exception:  # noqa: BLE001 -- 绝不让裁决中断整合
            return False

    def reconcile(self, new: Fact, live: list[Fact]) -> tuple[str, list[Fact]]:
        """返回 (action, invalidated_facts)。action 为 "add" 或 "duplicate"。"""
        same_slot = [f for f in live if f.slot == new.slot]

        # 完全相同的断言已存在 -> 去重（不新增 fact，也不失效化）
        for old in same_slot:
            if _norm(old.object) == _norm(new.object):
                return ("duplicate", [])

        # 用户权威：用户手动断言/编辑的 fact 拥有其槽位。自动提取的 fact 既不能覆盖它，也不能在
        # 单值槽位上作为竞争值与它并列——用户的修正必须生效。（`likes` 这类多值槽位仍正常累积。）
        if new.source != "user" and is_single_valued(new.predicate):
            if any(old.source == "user" for old in same_slot):
                return ("duplicate", [])

        invalidated: list[Fact] = []

        # 偏好反转：针对同一 object 的相反极性偏好会取代旧立场（"我喜欢跳舞" 之后 "我不喜欢跳舞"
        # -> 只保留讨厌那条）。两者 predicate 不同（likes 与 dislikes -> 不同槽位）且 object 相同
        # （所以语义路径会跳过它），否则这对矛盾会被漏掉、两者并存。多值累积不受影响：不同 object
        # （likes pizza + likes pasta）仍然并存；只有同一 object 上的 喜欢<->讨厌 翻转才被消解。
        # （PRD 治理: "修正否定" / "用户表达了与已有记忆相反的偏好".）
        new_pol = _pref_polarity(new.predicate)
        if new_pol is not None:
            for old in live:
                if _protected(old, new):
                    continue
                if _norm(old.subject) != _norm(new.subject) or _norm(old.object) != _norm(new.object):
                    continue
                old_pol = _pref_polarity(old.predicate)
                if old_pol is not None and old_pol != new_pol and new.valid_at >= old.valid_at:
                    _supersede(old, new)
                    invalidated.append(old)

        # 包含关系 / "Contained"（MemoryScope contra_repeat）：如果新断言的实义词是同槽位已有 fact
        # 的严格子集，新 fact 没有增量 -> 丢弃（去重）。如果严格超集，旧 fact 是同一断言较不完整的
        # 版本 -> 失效化它。这样剪掉冗余的近似重复，避免它们挤占检索到的上下文（Bet A：精确率）。
        # 限定在同槽位，使同一 predicate 下累积的值彼此保持独立。
        new_toks = _content_tokens(new.object)
        if new_toks:
            for old in same_slot:
                old_toks = _content_tokens(old.object)
                if not old_toks or old_toks == new_toks:
                    continue
                if new_toks < old_toks:  # 新 ⊂ 旧：被包含，无增量
                    return ("duplicate", [])
                if old_toks < new_toks and new.valid_at >= old.valid_at and not _protected(old, new):
                    _supersede(old, new)  # 新 ⊃ 旧：更完整
                    invalidated.append(old)

        if is_single_valued(new.predicate):
            _seen = {id(f) for f in invalidated}  # 包含关系已失效化的不再重复失效化
            # 1. exact-slot：同 (subject, predicate)，object 不同
            for old in same_slot:
                if id(old) in _seen or _protected(old, new):
                    continue
                if _norm(old.object) != _norm(new.object) and new.valid_at >= old.valid_at:
                    _supersede(old, new)
                    invalidated.append(old)

            # 2. semantic：subject 相同、嵌入相近（不同自由格式 predicate 下的同一属性）、
            #    object 不同，且新 fact 的 valid 时间不早于旧 fact。
            if self.embedder is not None and new.embedding:
                done = {id(f) for f in invalidated}
                new_prov = set(new.provenance)
                for old in live:
                    if (id(old) in done or old.slot == new.slot or _protected(old, new)
                            or new.source == "user"):
                        # 🔒 PIN GUARD（打地鼠式修复）：手动断言的（source="user"）fact 绝不能跨槽位
                        # 模糊取代无关的 fact——它只能更新同一槽位（走上面的 exact-slot 路径）。否则，
                        # 给某 subject 添加固定的 fact A，就会仅因为嵌入相近（subject 主导的相似度，
                        # 短中文 fact 尤甚）而废弃同一 subject 下无关的单值 fact B。LongMemEval
                        # 只写入提取出的 fact（没有 source="user"），所以这对 benchmark 零风险。
                        continue  # 由 exact-slot / 相同槽位 / 用户保护 / pin guard 处理
                    if _norm(old.subject) != _norm(new.subject) or _norm(old.object) == _norm(new.object):
                        continue
                    if not is_single_valued(old.predicate) or not old.embedding:
                        continue
                    # 同句共述守卫：从同一 episode 提取的两个 fact 是同时断言的，它们是互补属性
                    # （如同一句话里的 works_at 字节跳动 + job_title 后端开发），不是矛盾/更新。
                    # 嵌入路径容易把主题相近的短 fact 过度合并——中文尤其如此——所以绝不允许它
                    # 跨共享来源取代。真正的更新出现在更晚、独立的 episode 中。
                    if new_prov and new_prov & set(old.provenance):
                        continue
                    if new.valid_at < old.valid_at:
                        continue
                    sim = cosine(new.embedding, old.embedding)
                    if sim >= self.sim_threshold:
                        _supersede(old, new)
                        invalidated.append(old)
                    elif sim >= self.sim_threshold - self.ambiguous_band and self._llm_says_replaces(new, old):
                        # 相似度处于边界 -> 交给 LLM 裁决（仅在配置了 LLM 时）
                        _supersede(old, new)
                        invalidated.append(old)

        # 把 supersedes 指向我们取代的最新 fact（演化链的头部）
        if invalidated:
            new.supersedes = max(invalidated, key=lambda f: f.valid_at).id
        return ("add", invalidated)
