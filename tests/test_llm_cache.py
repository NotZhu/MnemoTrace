"""把提取器固定（pin）住，读路径的 A/B 对比衡量的才是代码而不是模型。

起因（2026-08-29 事故）：LOCOMO 上一次同题 A/B 报告 single-hop 在两个逐字节相同的渲染路径之间
丢了 5 题。它并没有回归——83 道单跳题里，没有任何一题在两次运行间产生相同的 context token 数，
因为 LLM 提取器每次都从相同的会话里提炼出不同的事实集合。那次实验里的每个 delta 都落在提取器
自身的方差之内。"""
from __future__ import annotations

import tempfile
import json
from mnemotrace import Memory

from mnemotrace.llm.cache import CachedLLM


class FlakyLLM:
    """替代真实提取器：同样的 prompt，每次调用返回不同的回复。"""

    model = "flaky-1"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, **kwargs) -> str:
        self.calls += 1
        return f"reply-{self.calls}"


def test_same_prompt_returns_the_same_reply():
    with tempfile.TemporaryDirectory() as d:
        inner = FlakyLLM()
        llm = CachedLLM(inner, d)
        first = llm.complete("extract facts from: Alice moved to Paris")
        again = llm.complete("extract facts from: Alice moved to Paris")
        assert first == again, "a pinned extractor must not vary between calls"
        assert inner.calls == 1, "the second call must be served from cache"
        assert llm.stats() == {"hits": 1, "misses": 1, "hit_rate": 0.5}


def test_cache_survives_a_new_process_object():
    """各次运行是独立进程；如果 pin 不能跨越进程存活，它就毫无价值。"""
    with tempfile.TemporaryDirectory() as d:
        first = CachedLLM(FlakyLLM(), d).complete("p")
        second_inner = FlakyLLM()
        second = CachedLLM(second_inner, d).complete("p")
        assert first == second
        assert second_inner.calls == 0, "the second run must not call the model at all"


def test_different_prompts_and_kwargs_do_not_collide():
    with tempfile.TemporaryDirectory() as d:
        llm = CachedLLM(FlakyLLM(), d)
        assert llm.complete("a") != llm.complete("b")
        assert llm.complete("a", system="s1") != llm.complete("a", system="s2")


def test_different_models_do_not_share_entries():
    """把 doubao 提炼的事实喂给 gpt-5.6 的运行，会毁掉这个缓存要保护的对比本身。"""
    with tempfile.TemporaryDirectory() as d:
        shared = FlakyLLM()  # 同一个模型对象，所以每次回复不同，缓存命中与否可见
        a = CachedLLM(shared, d, model_tag="model-a").complete("p")
        b = CachedLLM(shared, d, model_tag="model-b").complete("p")
        assert shared.calls == 2, "a different model tag must miss the cache"
        assert a != b
        # 每个 tag 各自重放自己的条目
        assert CachedLLM(shared, d, model_tag="model-a").complete("p") == a
        assert shared.calls == 2


def test_empty_replies_are_not_cached():
    """空回复是重试循环要处理的瞬态失败形态；把它固定进缓存会让一次抖动变成永久。"""

    class SometimesEmpty:
        model = "e"

        def __init__(self):
            self.calls = 0

        def complete(self, prompt, **kwargs):
            self.calls += 1
            return "" if self.calls == 1 else "real answer"

    with tempfile.TemporaryDirectory() as d:
        inner = SometimesEmpty()
        llm = CachedLLM(inner, d)
        assert llm.complete("p") == ""
        assert llm.complete("p") == "real answer"  # 走了重试，而不是从缓存重放


def test_unknown_attributes_fall_through_to_the_real_model():
    with tempfile.TemporaryDirectory() as d:
        assert CachedLLM(FlakyLLM(), d).model == "flaky-1"


def test_identity_registration_does_not_depend_on_extraction_order():
    import time
    from mnemotrace.llm import FakeLLM
    outputs = {
        "I like coffee": [{"subject":"user","predicate":"likes","object":"coffee"}],
        "I like hiking": [{"subject":"user","predicate":"likes","object":"hiking"}],
        "you can call me Evan": [{"subject":"user","predicate":"name","object":"Evan"}],
    }
    def complete(prompt, system):
        from mnemotrace.consolidate.planning import WINDOW_PREFIX
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        content = data["pending"][0]["content"]
        if content == "I like coffee":
            time.sleep(0.01)
        return json.dumps({"facts": outputs[content], "working": []})
    seen = []
    for _ in range(3):
        mem = Memory(llm=FakeLLM(handler=complete))
        for i, text in enumerate(outputs):
            mem.add(text, user_id="u1", session_id=f"s{i}", event_time=1_700_000_000.0 + i * 86400)
        mem.consolidate()
        seen.append(sorted(f.text for f in mem._all_facts()))
        assert mem._identity.get("u1") == "Evan"
    assert seen[0] == seen[1] == seen[2]
    assert all("coffee" in " ".join(rows) for rows in seen)
