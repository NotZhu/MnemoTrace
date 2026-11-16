"""Render complete evidence entries within a context budget."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextSection:
    """一个来源空间内的上下文分段。"""
    kind: str
    text: str
    space_id: str


def pack_sections(groups, budget, space_id):
    """按字符预算装入完整的证据条目。

    Args:
        groups: 按顺序排列的分段类型、标题和证据条目。
        budget: 上下文字符预算。
        space_id: 证据所属的空间 ID。

    Returns:
        已纳入的分段，以及是否有条目因预算被省略。
    """
    sections, used, limited = [], 0, False
    for kind, title, entries in groups:
        selected = []
        cost = len(title) + (2 if sections else 0)
        for entry in entries:
            if used + cost + 1 + len(entry) > budget:
                limited = True
                continue
            selected.append(entry)
            cost += 1 + len(entry)
        if selected:
            sections.append(ContextSection(kind, title + '\n' + '\n'.join(selected), space_id))
            used += cost
    return tuple(sections), limited


def render_sections(sections):
    """用空行连接上下文分段。

    Args:
        sections: 要渲染的上下文分段。

    Returns:
        组装后的上下文文本。
    """
    return '\n\n'.join(section.text for section in sections)
