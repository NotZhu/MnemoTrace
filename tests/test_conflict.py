"""冲突检测的单元测试（CLAUDE.md Bet C）。覆盖两个曾让事实层在知识更新场景失效的回归：
(1) 单值判定原本只是一个 3 项的 allow-list，导致大多数更新从不触发；
(2) 同一属性的自由格式 LLM 谓词（"attends_yoga" vs "does_yoga"）从不共享槽位。

这些测试直接驱动 ConflictResolver 并手工设置嵌入向量，因此是确定性的、无需模型。
`cosine` 原样传入；embedder 参数只是语义路径的"是否存在"开关。"""
from __future__ import annotations

from mnemotrace.consolidate.conflict import ConflictResolver, is_single_valued
from mnemotrace.types import Fact

_SENTINEL_EMBEDDER = object()  # 非 None：启用语义路径（并不会真的调用 .embed()）


def _fact(subject, predicate, obj, t, emb=None):
    return Fact(subject=subject, predicate=predicate, object=obj, user_id="u",
                valid_at=float(t), created_at=float(t), embedding=emb)


def test_single_valued_is_default_not_an_allowlist():
    # 旧 bug：只有 works_at/lives_in/name_is/favorite_* 被算作单值。现在陈述类谓词默认单值；
    # 只有显式的累加型集合才是多值。
    assert is_single_valued("studying")
    assert is_single_valued("current_city")
    assert is_single_valued("job_title")
    assert not is_single_valued("likes")
    assert not is_single_valued("owns")
    assert not is_single_valued("visited")
    # 目标/项目/事件会累加（根据诊断结果从单值判定中调了出去）
    assert not is_single_valued("goal")
    assert not is_single_valued("working_on")
    assert not is_single_valued("booked")


def test_exact_slot_update_invalidates_non_allowlist_predicate():
    r = ConflictResolver()  # 精确槽位匹配不需要 embedder
    old = _fact("user", "studying", "biology", 1)
    new = _fact("user", "studying", "chemistry", 2)
    action, inv = r.reconcile(new, [old])
    assert action == "add"
    assert inv == [old] and old.invalid_at == new.valid_at and new.supersedes == old.id


def test_multivalued_predicate_accumulates():
    r = ConflictResolver(_SENTINEL_EMBEDDER)
    old = _fact("user", "likes", "pizza", 1, emb=[1.0, 0.0])
    new = _fact("user", "likes", "pasta", 2, emb=[1.0, 0.0])  # 嵌入几乎相同，但 'likes' 是累加型
    action, inv = r.reconcile(new, [old])
    assert action == "add" and inv == []  # 两条都保留


def test_semantic_same_subject_different_predicate_supersedes():
    # yoga 案例：同一属性、不同自由格式谓词、不同取值、时间更晚。
    r = ConflictResolver(_SENTINEL_EMBEDDER, sim_threshold=0.80)
    old = _fact("user", "does_yoga", "twice a week", 1, emb=[1.0, 0.0])
    new = _fact("user", "attends_yoga", "three times a week", 2, emb=[0.99, 0.01])
    action, inv = r.reconcile(new, [old])
    assert inv == [old] and not old.is_live() and new.supersedes == old.id


def test_semantic_below_threshold_keeps_both():
    r = ConflictResolver(_SENTINEL_EMBEDDER, sim_threshold=0.80)
    old = _fact("user", "job", "teacher", 1, emb=[1.0, 0.0])      # 嵌入正交 -> 互不相关
    new = _fact("user", "hobby", "painting", 2, emb=[0.0, 1.0])
    action, inv = r.reconcile(new, [old])
    assert inv == []


def test_semantic_off_without_embedder():
    # 离线/hashing 路径：只有精确槽位会触发，所以谓词相近但不相同的一对不会被合并。
    r = ConflictResolver(None)
    old = _fact("user", "does_yoga", "twice a week", 1, emb=[1.0, 0.0])
    new = _fact("user", "attends_yoga", "three times a week", 2, emb=[1.0, 0.0])
    action, inv = r.reconcile(new, [old])
    assert inv == []


def test_semantic_never_supersedes_across_subjects():
    r = ConflictResolver(_SENTINEL_EMBEDDER, sim_threshold=0.50)
    old = _fact("wei", "lives_in", "Shenzhen", 1, emb=[1.0, 0.0])
    new = _fact("lin", "lives_in", "Beijing", 2, emb=[1.0, 0.0])  # 不同的人，嵌入却几乎相同
    action, inv = r.reconcile(new, [old])
    assert inv == []  # 不同主体永不合并
