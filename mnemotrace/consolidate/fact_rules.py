"""Pure normalization shared by write operations."""
import re


def object_key(fact):
    value = fact.object
    return value.strip().lower() if fact.object_kind == 'entity' else value


def same_assertion(existing, incoming):
    if existing.object_kind == incoming.object_kind:
        same_object = object_key(existing) == object_key(incoming)
    else:
        # Extraction may corroborate an unclassified manual fact without classifying it.
        same_object = (existing.source == 'user' and existing.object_kind == 'unknown'
                       and incoming.source == 'extracted' and existing.object == incoming.object)
    return (same_object and existing.slot == incoming.slot and existing.text == incoming.text
            and existing.valid_at == incoming.valid_at
            and (existing.valid_at is not None or bool(set(existing.provenance) & set(incoming.provenance))))


def _norm_predicate(p: str) -> str:
    p = p.strip().lower().replace("-", " ").replace("/", " ")
    p = re.sub(r"\s+", "_", p).strip("_")
    return p
