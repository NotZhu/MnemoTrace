"""Shared findings for inspecting facts, graph coverage, and embedder coverage."""
import re

from .localize import display_of
from .util import fmt_date

# 与控制台 Health 的颜色优先级一致。截取 limit 前排序，避免高优先级发现被低优先级占满首页。
AUDIT_KIND_PRIORITY = {
    "empty_value": 0, "embedder_blind": 0,
    "machine_token": 1, "slot_overflow": 1, "fragment": 1, "dangling_subject": 1,
    "unreduced_claim": 2, "code_artifact": 2,
    "orphan_entity": 3,
}

def finding(kind, why, action, *, fact=None, entity=""):
    row = {"kind": kind, "why": why, "action": action}
    if fact is not None:
        row.update(fact_id=fact.id, text=fact.text, display=display_of(fact), subject=fact.subject,
                   predicate=fact.predicate, object=fact.object, source=fact.source, valid_at=fact.valid_at,
                   valid_at_h=fmt_date(fact.valid_at))
    if entity:
        row["entity"] = entity
    return row


def fact_finding(f):
    obj, pred = f.object.strip(), f.predicate.strip()
    if "_" in obj and " " not in obj and len(obj) > 12:
        return finding("machine_token", f"object '{obj}' looks like a raw token, not a value",
            "edit into natural wording, or delete if meaningless", fact=f)
    elif obj.lower().replace("_", " ") == pred.lower().replace("_", " "):
        return finding("empty_value", f"object repeats the predicate ('{pred}')",
            "delete: it asserts nothing", fact=f)
    elif len(obj) > 120:
        return finding("unreduced_claim", f"object is {len(obj)} chars — a sentence, not a value",
            "shorten to the value, or delete", fact=f)
    elif re.search(r'[)"\'`\]]\s*$|^[("\'`\[]', obj) or "`" in obj:
        return finding("fragment", f"object '{obj[:40]}' carries stray punctuation — a clipped sentence",
            "rewrite as a plain value, or delete", fact=f)
    elif re.search(r"[/\\]|\.(py|ts|md|json|yaml|sh)\b", obj):
        return finding("code_artifact", f"object '{obj[:40]}' looks like a path or code, not a memory",
            "delete: source-code detail does not belong in a personal profile", fact=f)
    elif len(obj.split()) <= 2 and obj.isupper():
        return finding("empty_value", f"object '{obj}' is a bare token with no statement around it",
            "delete: it asserts nothing on its own", fact=f)
    elif f.subject.lower() in {"this", "that", "it", "these", "those", "there", "here"}:
        return finding("dangling_subject", f"subject is the pronoun '{f.subject}' — no identifiable entity",
            "delete, or rewrite naming who/what it refers to", fact=f)


class EmbedderCoverage:
    def __init__(self, embedder):
        from .embed import HashingEmbedder

        self.embedder = embedder
        self.exact = isinstance(embedder, HashingEmbedder)
        self.enabled = self.exact or (
            embedder.__class__.__name__ == "SentenceTransformerEmbedder"
            and "-en-" in str(getattr(embedder, "model_name", "") or "")
        )
        self.checked = self.rankable_chars = self.dark_chars = 0
        self.facts_blind = self.episodes_blind = 0

    def add(self, text, *, is_fact):
        if not self.enabled:
            return
        rankable = dark = 0
        for char in text:
            if char.isascii():
                rankable += char.isalnum()
            else:
                dark += char.isalpha()
        self.checked += 1
        self.rankable_chars += rankable
        self.dark_chars += dark
        if dark and dark >= rankable:
            if is_fact:
                self.facts_blind += 1
            else:
                self.episodes_blind += 1

    def finding(self, *, postgres=False):
        embedder = self.embedder
        checked = self.checked
        rankable_chars, dark_chars = self.rankable_chars, self.dark_chars
        facts_blind, episodes_blind = self.facts_blind, self.episodes_blind
        total_blind = facts_blind + episodes_blind
        if rankable_chars + dark_chars < 200:
            return None
        ratio = total_blind / checked if checked else 0.0
        mass_ratio = dark_chars / (rankable_chars + dark_chars)
        if ratio < 0.20 and mass_ratio < 0.20:
            return None
        ratio = max(ratio, mass_ratio)
        name = embedder.__class__.__name__
        pct = round(ratio * 100)
        if postgres:
            migrate = "MNEMOTRACE_EMBEDDER=multilingual"
            why = (f"{name} has limited ranking coverage for {pct}% of the space's content. "
                   "The lexical index also has limited coverage for these scripts.")
            action = f"configure a multilingual embedder and rebuild vectors: {migrate}"
        else:
            migrate = "MNEMOTRACE_EMBEDDER=multilingual MNEMOTRACE_REEMBED_ON_MISMATCH=1"
            why = (f"{pct}% of what this namespace holds is written in a script {name} cannot tokenize. "
                   "These memories have limited semantic and lexical coverage.")
            action = f"switch the server embedder and re-embed: {migrate} (or bge-small-zh)"
        return {
            "kind": "embedder_blind",
            "text": f"{name} cannot rank {pct}% of {checked} memories",
            "why": why,
            "action": action,
            "embedder": name,
            "model": getattr(embedder, "model_name", None),
            "blind": total_blind, "checked": checked, "ratio": ratio,
            "dark_chars": dark_chars, "rankable_chars": rankable_chars,
            "facts_blind": facts_blind, "episodes_blind": episodes_blind,
            "exact": self.exact, "recommended": "multilingual", "alternative": "bge-small-zh",
            "migrate": migrate,
        }



def embedder_blindness(embedder, fact_texts, source_texts):
    coverage = EmbedderCoverage(embedder)
    if coverage.enabled:
        for text in fact_texts:
            coverage.add(text, is_fact=True)
        for text in source_texts:
            coverage.add(text, is_fact=False)
    return coverage.finding()
