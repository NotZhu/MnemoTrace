"""共享的零依赖工具（id、时间、分词、向量计算）。"""
from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timedelta, timezone

# 纪元秒；一天的秒数（供新近度衰减使用）。
DAY = 86400.0

# fmt_datetime 的控制台展示时区（UTC+8）。eval/LLM 上下文的格式化保持 UTC（fmt_date）。
BEIJING = timezone(timedelta(hours=8))


def fmt_date(epoch: float) -> str:
    """纪元秒 -> 'YYYY-MM-DD'（UTC）。用于给 fact/chunk 打上真实日期。"""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"


def fmt_datetime(epoch: float) -> str:
    """纪元秒 -> 北京时间（UTC+8）的 'YYYY-MM-DD HH:MM:SS'。仅用于控制台展示：补上 fmt_date()
    丢掉的时分秒。fact 继承其 episode 的 event_time，因此时钟是真实的——但注意 LongMemEval
    的 session 时间戳只有分钟精度，秒位显示 ':00'；用户实时输入的 fact 则有真实秒。
    LLM 上下文构建器 + eval 刻意继续用 fmt_date：它们不需要时分秒，且依赖稳定的 UTC 纯日期标记。"""
    try:
        return datetime.fromtimestamp(epoch, tz=BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"


def now() -> float:
    """当前墙钟时间（纪元秒）。"""
    return time.time()


def time_key(value: float | None) -> float:
    """Sort unknown source times before known times without fabricating a timestamp."""
    return float('-inf') if value is None else value


def gen_id(prefix: str) -> str:
    """生成一个唯一 ID，带指定前缀。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stem(token: str) -> str:
    """粗糙的单数化，使 work/works、study/studies、colleague/colleagues 对齐。由词汇打分器
    和离线 embedder 共享，保证离线时语义与词汇信号一致。

    两条规则，均带长度门槛以放过短词基形（dies/lies/ties 去掉 -s 后仍是 'die'/'lie'/'tie'，
    因为它们不是 -y 词）：
      - 长度 > 4 且以 'ies' 结尾 -> 'y'  (studies -> study, cities -> city, carries -> carry)
      - 长度 > 3 且以 's' 结尾   -> ''    (works -> work, colleagues -> colleague)
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。对未归一化的向量也安全。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def recency(delta_seconds: float, tau_days: float) -> float:
    """[0, 1] 区间内的指数新近度衰减：现在为 1.0，随记忆变旧趋于 0。"""
    if tau_days <= 0:
        return 1.0
    return math.exp(-max(delta_seconds, 0.0) / (tau_days * DAY))


def minmax_normalize(scores: dict[str, float]) -> dict[str, float]:
    """把一组分数缩放到 [0, 1]。无差异输入 -> 全部 1.0。"""
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi - lo < 1e-12:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def extract_text(content: object) -> str:
    """把 'content' 字段的多种形状压平成纯文本：
      * 纯字符串                                    -> 原样返回
      * parts 列表（OpenAI 多模态/视觉）             -> 拼接文本部分
      * {'parts': [...]} / {'text': ...}（ChatGPT）  -> 取其中的文本
    非文本部分（图片、工具数据）被丢弃。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "parts" in content:
            return extract_text(content["parts"])
        for key in ("text", "content", "value"):
            if key in content:
                return extract_text(content[key])
        return ""
    if isinstance(content, (list, tuple)):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                # OpenAI 的 content part：{'type': 'text', 'text': '...'}
                if part.get("type") in (None, "text", "input_text", "output_text"):
                    out.append(extract_text(part.get("text") or part.get("content") or ""))
                # 其余：image_url / file / tool 部分 —— 跳过
        return "\n".join(p for p in out if p)
    return str(content)
