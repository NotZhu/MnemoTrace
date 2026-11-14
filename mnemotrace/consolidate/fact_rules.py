"""Pure normalization shared by write operations."""
import re


def object_key(fact):
    return (fact.object_id, None) if fact.object_id else (None, fact.object)


def assertion_key(fact, *, symmetric=False):
    if symmetric and fact.subject_id and fact.object_id:
        return fact.user_id, fact.relation_id, tuple(sorted((fact.subject_id, fact.object_id)))
    return fact.slot, object_key(fact)


def same_assertion(existing, incoming, *, match_time=True, bound_relation=False, symmetric=False):
    if (assertion_key(existing, symmetric=symmetric) != assertion_key(incoming, symmetric=symmetric)
            or (not bound_relation and existing.predicate != incoming.predicate)
            or existing.text != incoming.text):
        return False
    return not match_time or (existing.valid_at == incoming.valid_at
                              and (existing.valid_at is not None
                                   or bool(set(existing.provenance) & set(incoming.provenance))))


def _norm_predicate(p: str) -> str:
    p = p.strip().lower().replace("-", " ").replace("/", " ")
    p = re.sub(r"\s+", "_", p).strip("_")
    return p
