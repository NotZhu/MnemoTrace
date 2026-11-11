"""Render stored fact text without interpreting its meaning."""


def render_display(subject: str, predicate: str, obj: str, text: str = "", lang: str = "auto") -> str:
    return text.strip() or f"{subject} {predicate.replace('_', ' ')} {obj}".strip()


def display_of(fact, lang: str = "auto") -> str:
    return fact.display.strip() or render_display(fact.subject, fact.predicate, fact.object, fact.text, lang)
