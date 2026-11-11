"""Shared rules for user-authored facts and explicit fact corrections."""
from .fact_rules import same_assertion
from ..categories import normalize_categories, validate_object_kind
from ..types import Fact, validate_lifecycle
from ..util import now


class InvalidFactEditError(ValueError):
    """A user-supplied fact edit is invalid."""


def _manual_subject(subject):
    return 'user' if subject.strip().casefold() == 'user' else subject


def build_manual_fact(subject, predicate, object_, text, user_id, valid_at, sensitive, categories, object_kind,
                      cardinality="unknown", retention="unknown"):
    validate_fact_patch(subject, predicate, object_, text, required=True)
    labels = normalize_categories(categories) if categories is not None else []
    fact = Fact(subject=_manual_subject(subject), predicate=predicate, object=object_, text=text, user_id=user_id, source='user',
                valid_at=valid_at if valid_at is not None else now(),
                sensitive=True if sensitive is None else sensitive,
                sensitive_override=sensitive is not None,
                categories=labels, categories_source='user' if labels else 'unset', object_kind=object_kind,
                cardinality=cardinality, retention=retention)
    return fact


def validate_fact_patch(subject, predicate, object, text, *, required=False):
    """Content edits replace both representations; metadata-only patches need neither."""
    values = (subject, predicate, object, text)
    if (required or any(value is not None for value in values)) and any(
            not isinstance(value, str) or not value.strip() for value in values):
        raise InvalidFactEditError('Content edits require non-empty subject, predicate, object and text')


def apply_manual_edit(f, subject=None, predicate=None, object=None, sensitive=None, categories=None,
                      text=None, object_kind=None, cardinality=None, retention=None, *, at=None):
    """Apply a correction to one domain record and report whether its claim changed."""
    subject = _manual_subject(subject) if subject is not None else None
    labels = normalize_categories(categories) if categories is not None else None
    if object_kind is not None:
        validate_object_kind(object_kind)
    claim_changed = any(
        value is not None and value != getattr(f, field)
        for field, value in (("subject", subject), ("predicate", predicate), ("object", object), ('text', text))
    )
    object_changed = object is not None and object != f.object
    if object_kind is not None:
        f.object_kind = object_kind
    elif object_changed:
        f.object_kind = "unknown"
    if labels is not None or claim_changed:
        f.categories = labels or []
        f.categories_source = "user" if labels else "unset"
    new_cardinality = (f.cardinality if not claim_changed else "unknown") if cardinality is None else cardinality
    new_retention = (f.retention if not claim_changed else "unknown") if retention is None else retention
    validate_lifecycle(new_cardinality, new_retention)
    f.cardinality, f.retention = new_cardinality, new_retention
    if not claim_changed:
        # 表单会回传未变的内容字段；隐私/分类修改不代表用户断言正文，也不应撤销替代历史。
        if sensitive is not None:
            f.sensitive = sensitive
            f.sensitive_override = True
        return False
    f.subject, f.predicate, f.object, f.text = subject, predicate, object, text
    f.source = "user"
    f.revise_end_times(None, None, changed_at=now() if at is None else at)
    f.sensitive = True if sensitive is None else sensitive
    f.sensitive_override = sensitive is not None
    return True


def reconcile_manual(new, history, at):
    """Place a manually asserted value in the slot's valid-time history."""
    from ..util import time_key

    same_slot = [old for old in history if old.slot == new.slot and old.is_published(at)
                 and (old.expired_at is None or old.expired_at > at or old.invalid_at is not None)]
    effective = new.valid_at if new.valid_at is not None else at
    covering = [old for old in same_slot if old.is_valid_at(effective)]
    duplicate = next((old for old in covering if same_assertion(old, new)), None)
    if duplicate is not None:
        if new.categories_source == 'user' and duplicate.categories_source != 'user':
            duplicate.categories, duplicate.categories_source = new.categories, 'user'
        if new.sensitive_override and not duplicate.sensitive_override:
            duplicate.sensitive, duplicate.sensitive_override = new.sensitive, True
        return 'duplicate', [duplicate]
    if new.valid_at is None or new.cardinality != "single":
        return 'add', []

    def order(fact):
        return time_key(fact.valid_at), fact.created_at, fact.id

    successor = min((old for old in same_slot if old.valid_at is not None and old.valid_at > effective
                     and old.is_valid_at(old.valid_at)), key=order, default=None)
    changed = list(covering)
    for old in covering:
        expired_at = at if effective <= at and old.expired_at is None else old.expired_at
        old.revise_end_times(effective, expired_at, changed_at=at)
    if covering:
        new.supersedes = max(covering, key=order).id
    if successor is not None:
        new.invalid_at = successor.valid_at
        if new.invalid_at <= at:
            new.expired_at = at
        if successor.supersedes is None or any(old.id == successor.supersedes for old in covering):
            successor.supersedes = new.id
            changed.append(successor)
    return 'add', changed
