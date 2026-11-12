"""Construct synthetic facts for the offline workset."""
from .fact_rules import same_assertion
from ..categories import normalize_categories
from ..types import Fact
from ..util import now


def build_manual_fact(subject, predicate, object_, text, user_id, valid_at, sensitive, categories, retention="unknown", *, subject_id=None, object_id=None,
                      subject_type=None, object_type=None):
    if isinstance(subject, str) and subject.strip().casefold() == 'user':
        subject = 'user'
    labels = normalize_categories(categories) if categories is not None else []
    return Fact(subject=subject, predicate=predicate, object=object_, text=text, user_id=user_id, source='user',
                valid_at=valid_at if valid_at is not None else now(), sensitive=True if sensitive is None else sensitive,
                sensitive_override=sensitive is not None, categories=labels,
                categories_source='user' if labels else 'unset', retention=retention, subject_id=subject_id,
                object_id=object_id, subject_type=subject_type, object_type=object_type)


def reconcile_manual(new, history, at, *, explicit_valid_at):
    """Reuse an identical synthetic assertion already present in the workset."""
    same_slot = [old for old in history if old.slot == new.slot and old.is_published()]
    effective = new.valid_at if new.valid_at is not None else at
    covering = [old for old in same_slot if old.is_valid_at(effective)]

    def repeats(old):
        if explicit_valid_at or old.source != 'user':
            return same_assertion(old, new)
        # An implicit start changes on every request. Reuse an already effective manual assertion.
        return (same_assertion(old, new, match_time=False)
                and old.retention == new.retention)

    duplicate = next((old for old in covering if repeats(old)), None)
    if duplicate is not None:
        if new.categories_source == 'user' and duplicate.categories_source != 'user':
            duplicate.categories, duplicate.categories_source = new.categories, 'user'
        if new.sensitive_override and not duplicate.sensitive_override:
            duplicate.sensitive, duplicate.sensitive_override = new.sensitive, True
        return 'duplicate', [duplicate]
    return 'add', []
