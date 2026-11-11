"""Render complete evidence entries within a context budget."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextSection:
    kind: str
    text: str
    space_id: str


def pack_sections(groups, budget, space_id):
    """Groups contain an explicit kind, heading and a sequence of whole entries."""
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
    return '\n\n'.join(section.text for section in sections)
