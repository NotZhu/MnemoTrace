"""Pure normalization shared by write operations."""
import re


def object_key(fact):
    return (fact.object_id, None) if fact.object_id else (None, fact.object)


def same_assertion(existing, incoming, *, match_time=True):
    if (existing.withdrawn_at is not None or existing.slot != incoming.slot
            or object_key(existing) != object_key(incoming)
            or existing.text != incoming.text):
        return False
    return not match_time or (existing.valid_at == incoming.valid_at
                              and (existing.valid_at is not None
                                   or bool(set(existing.provenance) & set(incoming.provenance))))


def _norm_predicate(p: str) -> str:
    p = p.strip().lower().replace("-", " ").replace("/", " ")
    p = re.sub(r"\s+", "_", p).strip("_")
    return p
