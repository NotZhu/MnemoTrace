"""磁盘上的内容寻址缓存，垫在任意 LLM 前面 —— 让 A/B 实验度量的是代码，而不是模型。

为什么需要它：2026-08-29，我们在 LOCOMO 上用同题 A/B（本仓库的标准做法）测量一次读路径改动。
对比结果显示 single-hop 丢了 5 题。实际上并没有丢：核对两次运行的上下文大小后发现，83 道
single-hop 题里没有一道产生相同的 token 数，两次都对的那 68 道题平均相差 481 tokens
（极端值 -2597 / +3194）—— 而两次运行之间这段代码路径是逐字节一致的。抽取器是一个 LLM，每次
运行都会从同样的会话里蒸馏出不同的 fact 集合，那次实验里所有"回退"和"提升"都落在这个方差之内。

A/B 设计的前提是"除非代码改了它，否则上下文不变"。把 LLM 的回复钉死，这个前提才真正成立：
同样的 prompt -> 永远同样的回复，于是两次运行之间的差异只可能来自被测代码。抽取、摘要和
persona 构建都走 `LLM.complete`，所以包住这一个方法就把它们全部钉住了。

    from mnemotrace.llm.cache import CachedLLM
    llm = CachedLLM(make_llm("volcano:doubao-seed-1-6-flash-250615"), "data/llm_cache")

第一次运行填充缓存（成本和以前一样）；之后的运行回放缓存。answerer 和 judge 被基准框架刻意
*不*包缓存 —— 钉住被测对象没问题，钉住评分器会掩盖真实的回答方差。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional

from dataclasses import asdict

from .base import Completion, LLM, complete_result


class CachedLLM(LLM):
    """包装一个 LLM，使相同的 (prompt, kwargs) 永远返回相同的回复。

    缓存条目是 `path` 下以内容寻址的文件，因此缓存在跨进程、跨运行时都有效，同一基准的两个分片
    也可以共享它。未命中时落到被包装的模型并写回；损坏或不可读的条目按未命中处理而不是报错 ——
    被污染的缓存绝不能让一次基准运行失败。
    """

    def __init__(self, inner: LLM, path: str, model_tag: str = "") -> None:
        self.inner = inner
        self.path = path
        # 区分不同模型产生的条目：同一个 prompt 发给 doubao 和发给 gpt-5.6 得到的是不同的 fact，
        # 静默地拿一个顶替另一个，恰恰会污染这个类存在的意义——被保护的那次对比。
        self.model_tag = model_tag or getattr(inner, "model", inner.__class__.__name__)
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        os.makedirs(path, exist_ok=True)

    def _key(self, prompt: str, kwargs: dict) -> str:
        payload = json.dumps(
            {"m": str(self.model_tag), "p": prompt, "k": {k: str(v) for k, v in sorted(kwargs.items())}},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read(self, key: str) -> Optional[str]:
        try:
            with open(os.path.join(self.path, key), encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError):
            return None  # 缺失或不可读 -> 按未命中处理，绝不让运行失败

    def _write(self, key: str, value: str) -> None:
        target = os.path.join(self.path, key)
        tmp = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(value)
            os.replace(tmp, target)  # 原子写：读取者永远看不到写了一半的条目
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def complete(self, prompt: str, **kwargs) -> str:
        key = self._key(prompt, kwargs)
        cached = self._read(key)
        if cached is not None:
            with self._lock:
                self.hits += 1
            return cached
        value = self.inner.complete(prompt, **kwargs)
        with self._lock:
            self.misses += 1
        if value:  # 绝不缓存空回复：那正是重试循环要处理的瞬时失败形态
            self._write(key, value)
        return value

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}

    def complete_result(self, prompt: str, **kwargs) -> Completion:
        # Separate keys prevent old text-only entries from losing truncation metadata.
        key = self._key(prompt, {**kwargs, "_completion_record": "text-finish-usage"})
        cached = self._read(key)
        if cached is not None:
            try:
                result = Completion(**json.loads(cached))
            except (ValueError, TypeError):
                pass
            else:
                with self._lock:
                    self.hits += 1
                return result
        result = complete_result(self.inner, prompt, **kwargs)
        with self._lock:
            self.misses += 1
        if result.text:
            self._write(key, json.dumps(asdict(result), ensure_ascii=False))
        return result

    def __getattr__(self, name):
        # 代码里从模型对象上读取的其他属性（如 `.model`）透传给真实模型，
        # 这样被缓存包装的 LLM 仍然是即插即用的替代品。
        return getattr(self.inner, name)
