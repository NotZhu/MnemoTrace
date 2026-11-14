"""Construct synthetic facts for the offline workset."""
from .fact_rules import same_assertion
from ..categories import normalize_categories
from ..types import Fact
from ..util import now


def build_manual_fact(subject, predicate, object_, text, user_id, valid_at, sensitive, categories, retention="unknown", *, subject_id=None, object_id=None,
                      subject_type=None, object_type=None):
    labels = normalize_categories(categories) if categories is not None else []
    return Fact(subject=subject, predicate=predicate, object=object_, text=text, user_id=user_id, source='user',
                valid_at=valid_at if valid_at is not None else now(), sensitive=True if sensitive is None else sensitive,
                categories=labels, retention=retention, subject_id=subject_id,
                object_id=object_id, subject_type=subject_type, object_type=object_type)


def manual_duplicate(new, history, *, explicit_valid_at):
    """Reuse a matching assertion effective at the synthetic fact's start."""
    return next((old for old in history if old.is_valid_at(new.valid_at)
                 and same_assertion(old, new, match_time=explicit_valid_at)), None)
