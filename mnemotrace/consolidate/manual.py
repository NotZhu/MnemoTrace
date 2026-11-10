"""Shared rules for user-authored facts and explicit fact corrections."""
from ..localize import display_of
from ..categories import normalize_categories, validate_object_kind
from ..types import Fact, validate_lifecycle
from ..util import now
from .outcomes import OUTCOME_PREDICATES, split_outcome_text


class InvalidFactEditError(ValueError):
    """A user-supplied fact edit is invalid."""


def _manual_subject(subject):
    return 'user' if subject.strip().casefold() == 'user' else subject


def build_manual_fact(subject, predicate, object_, user_id, valid_at, sensitive, categories, object_kind,
                      cardinality="unknown", retention="unknown"):
    labels = normalize_categories(categories) if categories is not None else []
    fact = Fact(subject=_manual_subject(subject), predicate=predicate, object=object_, user_id=user_id, source='user',
                valid_at=valid_at if valid_at is not None else now(),
                sensitive=True if sensitive is None else sensitive,
                sensitive_override=sensitive is not None,
                categories=labels, categories_source='user' if labels else 'unset', object_kind=object_kind,
                cardinality=cardinality, retention=retention)
    return fact


def apply_manual_edit(f, subject=None, predicate=None, object=None, sensitive=None, categories=None,
                      statement=None, object_kind=None, cardinality=None, retention=None, *, at=None):
    """Apply a correction to one domain record and report whether its claim changed."""
    subject = _manual_subject(subject) if subject is not None else None
    labels = normalize_categories(categories) if categories is not None else None
    if object_kind is not None:
        validate_object_kind(object_kind)
    if statement is not None:
        if any(value is not None for value in (subject, predicate, object)):
            raise InvalidFactEditError("statement cannot be combined with subject, predicate or object")
        statement = statement.strip()
        if not statement:
            raise InvalidFactEditError("statement must not be blank")
        # 属性的 object 是关系的取值，整句写入会把画像和图边变成错误的三元组。
        if f.predicate.lower() not in OUTCOME_PREDICATES:
            raise InvalidFactEditError(
                "statement edits are only supported for session outcome facts; "
                "edit subject, predicate and object for attribute facts"
            )
    claim_changed = any(
        value is not None and value != getattr(f, field)
        for field, value in (("subject", subject), ("predicate", predicate), ("object", object))
    ) or (statement is not None and statement != display_of(f) and statement != f.text)
    object_changed = (object is not None and object != f.object) or (statement is not None and claim_changed)
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
    if subject is not None:
        f.subject = subject
    if predicate is not None:
        f.predicate = predicate
    if object is not None:
        f.object = object
    # outcome 不是三元组：它的 subject 是 session id，object 是整句话，因此按通用的
    # "subject predicate object" 重建会嵌入 `s-2026-08-31 lesson ...` 并丢掉作为检索依据的
    # 依据子句（人们按教训的成因搜索教训）。
    if statement is not None:
        f.object = statement
        _, why = split_outcome_text(f.text)
        f.text = f"{statement} （依据：{why}）" if why else statement
        f.display = statement
    elif f.predicate.lower() in OUTCOME_PREDICATES:
        _, why = split_outcome_text(f.text)
        f.text = f"{f.object} （依据：{why}）" if why else f.object
    else:
        f.text = f"{f.subject} {f.predicate.replace('_', ' ')} {f.object}".strip()
    # display_of() 优先用 `display` 而非 `text`，因此过期的 display 会让所有界面在用户被告知
    # 保存成功后仍渲染编辑前的措辞。这不只影响 outcome。outcome 的 display 就是它的 object
    # （历史结论记录保存 `display=statement`），可以直接重新赋值。其他所有 fact 的 display 都是
    # extractor 生成的完整句子（"Wei works at Tencent"），若把裸 object 赋给 display 就会渲染成
    # "Moonshot AI"——本功能要修复的碎片化问题又从属性侧回来了。正确做法是清空 display，让
    # display_of() 回退到 render_display(subject, predicate, object)——没有原语言展示文本的 fact
    # （display=""）本来就是这么渲染的。
    # 以"text 是否被重建"为门控条件，而非"object 是否变化"：重命名 predicate（finding ->
    # lesson）或 subject 同样会重写 text，留下旧 display 是同一种过期渲染 bug。
    if statement is None and (subject is not None or predicate is not None or object is not None) \
            and (getattr(f, "display", "") or "").strip():
        f.display = f.object if f.predicate.lower() in OUTCOME_PREDICATES else ""
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
    if any(old.object.strip().casefold() == new.object.strip().casefold() for old in covering):
        return 'duplicate', []
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
