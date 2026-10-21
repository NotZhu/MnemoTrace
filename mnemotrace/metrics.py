"""在线服务指标（AGENTS.md Bet D 应用于服务化）：把离线报告的三元组——延迟 + token + 调用量
——放到运行中的服务上测量。写入路径的 System-1 目标是 <50ms，读取路径目标是 <100ms（「架构概览与代码导航」）；
没有实时分位数，这些目标就只是口号，不是数字。

纯标准库，且刻意只做聚合：操作延迟、计数器和 token 总量——绝不含用户内容、命名空间名或
查询文本，因此 /metrics 端点可以像 /health 一样保持开放。
"""
from __future__ import annotations

import functools
import threading
import time
from collections import Counter, defaultdict, deque
from typing import Optional


def _pct(sorted_vals: list[float], q: float) -> float:
    """对已排序样本做最近秩百分位（不插值；对 SLO 仪表盘足够）。"""
    return sorted_vals[min(int(q * (len(sorted_vals) - 1) + 0.5), len(sorted_vals) - 1)]


class Metrics:
    """线程安全、内存固定的指标：每个操作维护最近延迟的滑动窗口（分位数反映*当前*行为，
    而非全历史混合）、单调计数器，以及 token 节省总量。"""

    def __init__(self, window: int = 512) -> None:
        self._lock = threading.Lock()
        self._lat: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self._counts: Counter = Counter()
        self._ctx_tokens = 0  # 已服务的拼装（lean）上下文 token 总数
        self._full_tokens = 0  # full-history 基线 token 总数（顺带计算时才记录）
        self._with_baseline = 0  # 同时测得两侧的 recall 次数（节省比的分母）
        self._started = time.time()

    # --- 记录 -----------------------------------------------------------
    def observe(self, op: str, seconds: float) -> None:
        with self._lock:
            self._lat[op].append(seconds)
            self._counts[op] += 1

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    def tokens(self, context: int, full: Optional[int] = None) -> None:
        """记录一次所服务上下文的大小；`full`（full-history token 数）仅在调用方顺带算出时记录
        ——节省比由两侧都被测量的记录对推导。"""
        with self._lock:
            self._ctx_tokens += int(context)
            if full is not None:
                self._full_tokens += int(full)
                self._with_baseline += 1

    # --- 读取 -------------------------------------------------------------
    def snapshot(self) -> dict:
        """可 JSON 化的聚合视图（/metrics 载荷）。只有数字——构造上就不含任何用户数据。"""
        with self._lock:
            ops = {}
            for op, window in self._lat.items():
                if not window:
                    continue
                s = sorted(window)
                ops[op] = {
                    "n": self._counts[op],
                    "p50_ms": round(_pct(s, 0.50) * 1000, 2),
                    "p95_ms": round(_pct(s, 0.95) * 1000, 2),
                    "avg_ms": round(sum(s) / len(s) * 1000, 2),
                    "max_ms": round(s[-1] * 1000, 2),
                    "window": len(s),
                }
            counts = {k: v for k, v in self._counts.items() if k not in ops}
            tokens = {
                "context_total": self._ctx_tokens,
                "full_total": self._full_tokens,
                "recalls_with_baseline": self._with_baseline,
                # 在线的"token 少约 8 倍"数字：full-history 成本 / 所服务上下文成本，
                # 在两侧都被测量的 recall 上计算。至少有一对样本前为 None。
                "savings_ratio": (round(self._full_tokens / self._ctx_tokens, 2)
                                  if self._ctx_tokens and self._full_tokens else None),
            }
            return {"uptime_s": round(time.time() - self._started, 1),
                    "ops": ops, "counts": counts, "tokens": tokens}


def timed(op: str):
    """MemoryService 方法的装饰器：把调用的墙钟耗时记录在 `op` 名下。调用时才取
    `self.metrics`，因此不给构造增加任何负担，也能在服务重新初始化后继续工作。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(self, *args, **kwargs)
            finally:
                m = getattr(self, "metrics", None)
                if m is not None:
                    m.observe(op, time.perf_counter() - t0)
        return wrapper
    return deco
