"""可选开启的 ANN 候选生成（AGENTS.md Bet E，规模化读路径）。本文件验证的保证是：它默认关闭；
且当小存储的 ANN 候选池覆盖全部事实时，候选集等价于全量扫描——所以对小用户来说开启与否没有区别
（这是可以离线验证的"行为保持"性质；规模化下的排序质量交给 eval 把关）。使用内存 store，因此无需
LanceDB。

固定合成模型响应确保事实真正落库，避免候选集等价性检查仅比较空结果。"""
from __future__ import annotations

from mnemotrace.config import Config
from mnemotrace.embed import HashingEmbedder
from tests.model_fixtures import MemoryEngine

_TEXT = "My name is Wei and I live in Shenzhen."  # 固定响应包含 (Wei, lives_in, Shenzhen)


def _mem(ann: bool) -> MemoryEngine:
    m = MemoryEngine(config=Config(ann_candidates=ann), embedder=HashingEmbedder(64))
    m.add(_TEXT, user_id="u", consolidate=True)
    return m


def _spo(ranked):
    return [(f.subject, f.predicate, f.object) for f, _ in ranked]


def test_ann_candidates_off_by_default():
    assert Config().ann_candidates is False


def test_ann_path_retrieves_the_fact():
    ranked, _ = _mem(ann=True).retriever.retrieve("where do I live", "u")
    assert ranked, "the ANN candidate path should return candidates"
    assert any("shenzhen" in ((f.object or "") + " " + f.text).lower() for f, _ in ranked)


def test_ann_equals_full_scan_on_small_store():
    # 事实很少（< ann_pool）-> ANN 候选池覆盖全部 -> 候选集相同 -> 排序相同
    full, ann = _mem(ann=False), _mem(ann=True)
    for query in ("where do I live", "Wei", "Shenzhen"):
        assert _spo(full.retriever.retrieve(query, "u")[0]) == _spo(ann.retriever.retrieve(query, "u")[0]), \
            f"opt-in changed ranking for {query!r} on a small store"
    # 非空转：事实确实被检索到了（否则上面的相等可能只是 [] == []）
    assert _spo(ann.retriever.retrieve("where do I live", "u")[0])
