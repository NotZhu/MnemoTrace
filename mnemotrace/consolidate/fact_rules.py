"""Pure normalization shared by write operations."""
import re


def object_key(fact):
    value = fact.object
    return value.strip().lower() if fact.object_kind == 'entity' else value


def same_assertion(first, second):
    both_entities = first.object_kind == second.object_kind == 'entity'
    same_object = object_key(first) == object_key(second) if both_entities else first.object == second.object
    return (first.slot == second.slot and same_object
            and first.text == second.text and first.valid_at == second.valid_at
            and (first.valid_at is not None or bool(set(first.provenance) & set(second.provenance))))


def _norm_predicate(p: str) -> str:
    p = p.strip().lower().replace("-", " ").replace("/", " ")
    p = re.sub(r"\s+", "_", p).strip("_")
    return p
