"""规则优先的分类 + 敏感标记（feature ⑤）。

确定性且零成本——路径上不调用 LLM（之后可以像冲突裁决一样叠加一层 LLM 升级处理）。为每个 fact
打上粗粒度的领域 CATEGORY（用于组织与过滤）和 SENSITIVE 标志（PII / 健康 / 财务 / 凭据 /
受保护属性）。默认情况下，敏感记忆在共享 / 导出场景中被隐去——这是记忆产品在隐私治理上的通用收益。
"""
from __future__ import annotations

import re

from ._rule_match import compile_keywords, matches_keywords, normalize_predicate

# 明确的关系先于自由文本线索；例如雇主名 Storage Labs 不能把 works_at 变为身份信息。
_PREDICATES: dict[str, tuple[str, ...]] = {
    "健康": ("has_disease", "medical_condition", "allergic_to", "allergic", "allergy",
             "medication", "takes_medication", "blood_type", "health", "symptom", "symptoms"),
    "财务": ("salary", "income", "wage", "bank_account", "credit_card", "debt", "net_worth",
             "mortgage", "invests_in"),
    "身份": ("name", "age", "age_range", "gender", "sex", "birthday", "born", "born_in",
             "born_on", "birth_date", "birthplace", "nationality", "occupation", "job", "job_title",
             "profession", "works_as", "education", "degree", "id_number", "passport", "ssn"),
    "位置": ("lives_in", "lives_at", "home", "address", "based_in", "located", "located_in"),
    "关系": ("married", "married_to", "spouse", "partner", "child", "has_child", "has_children",
             "children", "kids", "family", "friend", "friends_with", "colleague", "boss", "relationship"),
    "工作": ("works_at", "works_on", "works_in", "employer", "company", "project"),
    "偏好": ("like", "likes", "love", "loves", "enjoy", "enjoys", "prefer", "prefers",
             "favorite", "favourite", "dislike", "dislikes", "hate", "hates", "avoid", "avoids",
             "interested_in", "fan_of", "fond_of", "not_into", "does_not_like", "disinterested_in"),
    "事件": ("visit", "visits", "visited", "went_to", "traveled_to", "travelled_to", "been_to",
             "bought", "ordered", "attended", "plan", "plans", "plans_to", "booked"),
}

# 未知关系才使用这些内容线索；同时命中多个类别时沿用原有顺序。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("健康", ("allergy", "allergies", "allergic", "disease", "diseases", "medical_condition",
              "medical_conditions", "health_condition", "health_conditions",
              "diagnosis", "diagnoses", "diagnosed", "diagnostic", "medication", "medications",
              "symptom", "symptoms", "pregnant", "pregnancy", "disability", "disabilities", "disabled",
              "diabetes", "diabetic", "blood_type", "health", "病", "疾病", "病情", "过敏", "怀孕",
              "孕期", "孕妇", "残疾", "症", "症状", "药", "药物", "用药", "服药", "吃药",
              "住院", "健康", "糖尿病")),
    ("财务", ("salary", "income", "wage", "bank", "credit_card", "debt", "net_worth", "mortgage",
              "invest", "invests", "invested", "investing", "investment", "investments",
              "工资", "收入", "薪", "薪资", "薪水", "银行", "信用卡", "负债", "存款", "理财")),
    ("身份", ("name", "age", "gender", "sex", "birthday", "born", "nationality", "occupation", "job",
              "profession", "education", "degree", "id_number", "passport", "ssn",
              "姓名", "年龄", "性别", "生日", "国籍", "职业", "学历", "身份证", "护照")),
    ("位置", ("lives_in", "home", "address", "based_in", "located", "poi", "住", "居住", "住在",
              "住所", "地址", "常住", "住北京")),
    ("关系", ("married", "spouse", "partner", "child", "kids", "family", "friend", "colleague", "boss",
              "relationship", "配偶", "孩子", "家人", "朋友", "同事")),
    ("工作", ("works_at", "employer", "company", "project", "works_on", "团队", "公司", "项目")),
    ("偏好", ("like", "likes", "love", "loves", "enjoy", "enjoys", "prefer", "prefers", "favorite",
              "favourite", "dislike", "dislikes", "hate", "hates", "avoid", "avoids", "interested",
              "fan_of", "喜欢", "讨厌", "偏好", "最爱")),
    ("事件", ("visit", "visits", "visited", "went", "trip", "trips", "bought", "ordered", "attended",
              "plan", "plans", "planned", "planning", "booked", "去", "去过", "去了", "买", "购买",
              "买了", "买水果", "计划")),
]
_CATEGORY_MATCHERS = [(category, compile_keywords(keywords)) for category, keywords in _CATEGORY_RULES]

# 敏感性：predicate 或内容命中受保护/PII 类别。健康、财务、凭据以及法律上的受保护属性
# （宗教 / 政治 / 性取向）默认视为敏感。
_SENSITIVE_KW: tuple[str, ...] = (
    # 健康类
    "allerg", "disease", "diagnos", "medication", "diabet", "hiv", "cancer", "depress", "anxiety",
    "pregnan", "disab", "mental", "blood_type", "symptom", "病", "过敏", "怀孕", "抑郁", "残疾", "药",
    # 财务类
    "salary", "income", "wage", "bank_account", "credit_card", "debt", "net_worth", "mortgage",
    "工资", "薪资", "收入", "银行卡", "信用卡", "负债", "存款",
    # 凭据 / 证件号
    "password", "passcode", "ssn", "passport", "id_number", "secret", "密码", "身份证", "护照", "密钥",
    # 受保护属性
    "religion", "religious", "christian", "muslim", "buddhis", "political", "gay", "lesbian", "bisexual",
    "sexual_orientation", "宗教", "政治", "性取向",
)

_SENSITIVE_PREDS = {"salary", "income", "password", "ssn", "passport", "id_number", "religion",
                    "political_affiliation", "sexual_orientation", "medical_condition", "allergic_to",
                    "has_disease", "blood_type", "bank_account", "credit_card"}


def _hay(predicate: str, obj: str, text: str) -> str:
    return f"{predicate} {obj} {text}".lower()


def _predicate_category(predicate: str) -> str:
    p = normalize_predicate(predicate)
    for category, predicates in _PREDICATES.items():
        if p in predicates:
            return category
    if any(p.startswith(prefix + "_") for prefix in _PREDICATES["偏好"]):
        return "偏好"
    return ""


def classify(predicate: str, obj: str = "", text: str = "") -> tuple[str, bool]:
    """返回 (category, sensitive)；正文只用于敏感检测，不能提供类别证据。"""
    p = predicate.lower()
    hay = _hay(p, obj, text)

    category = _predicate_category(p)
    if not category:
        category = next((cat for cat, kws in _CATEGORY_MATCHERS if matches_keywords(obj, kws)), "其他")

    sensitive = (
        p in _SENSITIVE_PREDS
        or any(k in hay for k in _SENSITIVE_KW)
        # 连续数字，形似账号/证件号/卡号
        or bool(re.search(r"\b\d{6,}\b", obj))
    )
    return category, sensitive


def classify_fact(fact) -> None:
    """原地设置 fact.category / fact.sensitive（幂等——仅在未设置时填充）。"""
    if getattr(fact, "category", ""):
        return
    cat, sens = classify(fact.predicate, fact.object, fact.text + "\n" + getattr(fact, "display", ""))
    fact.category = cat
    # 绝不把用户已标记的敏感标志降级
    fact.sensitive = bool(getattr(fact, "sensitive", False) or sens)
