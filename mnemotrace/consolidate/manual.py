"""Shared rules for user-authored facts and explicit fact corrections."""
from ..localize import display_of
from ..topics import validate_topic
from ..types import Fact
from ..util import now
from .classify import classify, classify_fact
from .outcomes import OUTCOME_CATEGORY, OUTCOME_PREDICATES, split_outcome_text
from .topic_rules import resolve_topic


class InvalidFactEditError(ValueError):
    """A user-supplied fact edit is invalid."""


def build_manual_fact(subject, predicate, object_, user_id, valid_at, sensitive, category, topic):
    fact = Fact(subject=subject, predicate=predicate, object=object_, user_id=user_id, source='user',
                valid_at=valid_at if valid_at is not None else now(),
                topic=topic if topic is not None else 'unknown',
                topic_source='user' if topic is not None else 'unset')
    if topic is None:
        fact.topic, fact.topic_source = resolve_topic(predicate, object_)
    classify_fact(fact)
    if category is not None:
        fact.category = category
    if sensitive is not None:
        fact.sensitive = sensitive
    return fact


def apply_manual_edit(f, subject=None, predicate=None, object=None, sensitive=None, category=None,
                      statement=None, topic=None, reset_topic=False):
    """Apply a correction to one domain record and report whether its claim changed."""
    if not isinstance(reset_topic, bool):
        raise InvalidFactEditError("reset_topic must be a boolean")
    if topic is not None:
        if reset_topic:
            raise InvalidFactEditError("topic cannot be combined with reset_topic")
        # 此入口把主题来源固定为 user；允许人工明确选择 unknown。
        validate_topic(topic, "user")
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
    if not claim_changed:
        # 表单会回传未变的内容字段；隐私/分类修改不代表用户断言正文，也不应撤销替代历史。
        if sensitive is not None:
            f.sensitive = sensitive
        if category is not None:
            f.category = category
        if topic is not None:
            f.topic, f.topic_source = topic, "user"
        elif reset_topic:
            # 即使结论使用三元组式默认正文，也不能将 session 主语带入主题判断。
            why = split_outcome_text(f.text)[1] if f.predicate.lower() in OUTCOME_PREDICATES else ""
            f.topic, f.topic_source = resolve_topic(f.predicate, f"{f.object} {why}")
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
    f.invalid_at = None  # 用户编辑使其重新生效
    f.expired_at = None
    # 依据编辑后的内容重新分类，再应用显式的用户覆盖
    f.category, f.sensitive = classify(f.predicate, f.object, f.text)
    # 结论被纠正后仍是结论。classify() 只认识属性类目，缺了这一步，一次编辑就会把 fact
    # 悄悄降级出 Journal。
    if f.predicate.lower() in OUTCOME_PREDICATES:
        f.category = OUTCOME_CATEGORY
    if category is not None:
        f.category = category
    if sensitive is not None:
        f.sensitive = sensitive
    # 内容变化使原主题失去依据；同次编辑明确指定的新主题优先。
    if topic is not None:
        f.topic, f.topic_source = topic, "user"
    else:
        why = split_outcome_text(f.text)[1] if f.predicate.lower() in OUTCOME_PREDICATES else ""
        f.topic, f.topic_source = resolve_topic(f.predicate, f"{f.object} {why}")
    return True
