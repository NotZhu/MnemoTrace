"""批量导入连接器 —— 把外部聊天/历史记录导出规范化为 忆痕 会话。

`mnemotrace.connectors` 里的所有东西都是**纯标准库**（无第三方依赖），因此在零配置安装下也能导入历史
（AGENTS.md 「代码导航」）。*解析器*把原始导出 —— ChatGPT 的 `conversations.json`、OpenAI `messages` 数组、
JSONL 日志、纯 transcript —— 转换成 `ImportSession` 列表，再由 `Memory.import_messages()` 用少量批量
嵌入调用 + 一次 System-2 整合完成导入（比逐条 `add()` 快 10 倍以上）。

这个接缝填补的空白：此前唯一的批量加载器是仅供 eval 使用的 `seed_console.py`。这是 「代码导航」 采纳层需要的、
面向用户的"自带历史"连接器。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 规范化模型 —— 所有解析器都输出这些类型，Memory.import_messages() 因此只需
# 面对一种形状，与来源格式无关。
# ---------------------------------------------------------------------------


@dataclass
class ImportMessage:
    """源导出中的一轮对话/一条消息。"""

    content: str
    role: str = "user"  # 作者角色：user | assistant | system | tool | ...
    event_time: Optional[float] = None  # epoch 秒（若来源带有时间）
    metadata: dict = field(default_factory=dict)


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

_DATE_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")


def to_epoch(value: Any) -> Optional[float]:
    """尽力把时间戳转成 epoch 秒。接受 epoch 整数/浮点数（自动识别毫秒/微秒/纳秒量级）、ISO-8601
    字符串（'2024-01-15T10:30:00Z'）和 'YYYY-MM-DD'。找不到可解析的内容时返回 None
    （调用方随后回退到合成时钟）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # 防御：Python 中 bool 是 int 的子类
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # 启发式换算：把明显过大的数值当作 epoch 起的 ms / µs / ns。
        while v > 1e12:
            v /= 1000.0
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 数字字符串 → epoch 秒
        try:
            return to_epoch(float(s))
        except ValueError:
            pass
        # ISO-8601（容忍结尾的 'Z'）
        iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
        # 在字符串中宽松匹配 'YYYY-MM-DD'
        m = _DATE_RE.search(s)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                return datetime(y, mo, d, tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
    return None


def extract_text(content: Any) -> str:
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
