"""批量导入连接器 —— 把外部聊天/历史记录导出规范化为 忆痕 会话。

`mnemotrace.connectors` 里的所有东西都是**纯标准库**（无第三方依赖），因此在零配置安装下也能导入历史
（AGENTS.md 「代码导航」）。*解析器*把原始导出 —— ChatGPT 的 `conversations.json`、OpenAI `messages` 数组、
JSONL 日志、纯 transcript —— 转换成 `ImportSession` 列表，再由 `connectors.dialogue.import_messages()`
按原始顺序逐条保存消息，并通过统一窗口执行历史抽取。

这个接缝填补的空白：此前唯一的批量加载器是仅供 eval 使用的 `seed_console.py`。这是 「代码导航」 采纳层需要的、
面向用户的"自带历史"连接器。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..util import extract_text as extract_text

# ---------------------------------------------------------------------------
# 规范化模型 —— 所有解析器都输出这些类型，connectors.dialogue.import_messages() 因此只需
# 面对一种形状，与来源格式无关。
# ---------------------------------------------------------------------------


@dataclass
class ImportMessage:
    """源导出中的一轮对话/一条消息。"""

    content: str
    role: str = "user"  # 作者角色：user | assistant | system | tool | ...
    event_time: Optional[float] = None  # epoch 秒（若来源带有时间）
    metadata: dict = field(default_factory=dict)
    message_id: Optional[str] = None


@dataclass
class ImportSession:
    """一段会话/线程：共享一个会话 id 的一组有序消息。"""

    session_id: str
    messages: list[ImportMessage] = field(default_factory=list)
    event_time: Optional[float] = None  # 会话开始时间；默认取第一条带时间戳的消息
    title: str = ""
    metadata: dict = field(default_factory=dict)

    def start_time(self) -> Optional[float]:
        """会话的世界时间：显式的开始时间，否则取第一条带时间戳的消息。"""
        if self.event_time is not None:
            return self.event_time
        for m in self.messages:
            if m.event_time is not None:
                return m.event_time
        return None

    def to_text(self, roles: bool = True) -> str:
        """把各轮渲染成单个 episode 正文。`roles=True` 时每轮前缀说话者（'user: ...'），
        提取器/检索器把它当作真实文本来读 —— 与 eval harness 导入会话的方式一致（每个会话一个
        episode，各轮拼接）。"""
        lines = []
        for m in self.messages:
            text = (m.content or "").strip()
            if not text:
                continue
            lines.append(f"{m.role}: {text}" if roles else text)
        body = "\n".join(lines)
        if self.title and self.title.strip():
            body = f"# {self.title.strip()}\n{body}"
        return body


# ---------------------------------------------------------------------------
# 共享辅助函数（时间 + 内容规范化），被所有解析器复用。
# ---------------------------------------------------------------------------

_TIME_UNITS = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}


def to_epoch(value: Any, *, unit: str = "s") -> Optional[float]:
    """Read epoch values in the declared unit or ISO timestamps with a timezone."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            try:
                value = value.strip()
                stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
                return stamp.timestamp() if stamp.utcoffset() is not None else None
            except (ValueError, OverflowError, OSError):
                return None
    if isinstance(value, (int, float)):
        try:
            seconds = value / _TIME_UNITS[unit]
            if -62135596800 <= seconds < 253402300800:
                return seconds
        except OverflowError:
            pass
    return None


def record_time(record: dict, *keys: str) -> Optional[float]:
    """Resolve protocol fields, including explicit unit suffixes."""
    for key in keys:
        for unit in _TIME_UNITS:
            value = record.get(f"{key}_{unit}")
            if value is not None:
                return to_epoch(value, unit=unit)
        value = record.get(key)
        if value is not None and value != "":
            return to_epoch(value)
    return None




def load_json(data: Any) -> Any:
    """接受已解析的对象、JSON 文本或 bytes；返回 Python 对象。"""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    if isinstance(data, str):
        return json.loads(data)
    return data


def iter_jsonl(text: str):
    """从 JSON-Lines 文本逐个产出解析后的对象（每个非空行一个 JSON 值）。"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
