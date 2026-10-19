"""规则优先的分类 + 敏感标记（feature ⑤）。

确定性且零成本——路径上不调用 LLM（之后可以像冲突裁决一样叠加一层 LLM 升级处理）。为每个 fact
打上粗粒度的领域 CATEGORY（用于组织与过滤）和 SENSITIVE 标志（PII / 健康 / 财务 / 凭据 /
受保护属性）。默认情况下，敏感记忆在共享 / 导出场景中被隐去——这是记忆产品在隐私治理上的通用收益。
"""
from __future__ import annotations

import re

# category -> (predicate 关键词, object/正文关键词)。命中首个即返回，顺序即优先级。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("健康", ("allerg", "disease", "condition", "diagnos", "medication", "symptom", "pregnan", "disab",
              "diabet", "blood_type", "health", "病", "过敏", "怀孕", "孕", "残疾", "症", "药")),
    ("财务", ("salary", "income", "wage", "bank", "credit_card", "debt", "net_worth", "mortgage",
              "invest", "工资", "收入", "薪", "银行", "信用卡", "负债", "存款", "理财")),
    ("身份", ("name", "age", "gender", "sex", "birthday", "born", "nationality", "occupation", "job",
              "profession", "education", "degree", "id_number", "passport", "ssn",
              "姓名", "年龄", "性别", "生日", "国籍", "职业", "学历", "身份证", "护照")),
    ("位置", ("lives_in", "home", "address", "based_in", "located", "poi", "住", "地址", "常住")),
    ("关系", ("married", "spouse", "partner", "child", "kids", "family", "friend", "colleague", "boss",
              "relationship", "配偶", "孩子", "家人", "朋友", "同事")),
    ("工作", ("works_at", "employer", "company", "project", "works_on", "团队", "公司", "项目")),
    ("偏好", ("like", "love", "enjoy", "prefer", "favorite", "favourite", "dislike", "hate", "avoid",
              "interested", "fan_of", "喜欢", "讨厌", "偏好", "最爱")),
    ("事件", ("visit", "went", "trip", "bought", "ordered", "attended", "plan", "booked", "去", "买", "计划")),
]

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


def classify(predicate: str, obj: str = "", text: str = "") -> tuple[str, bool]:
    """返回 (category, sensitive)。纯规则，作用于 predicate + object + 渲染文本。"""
    p = predicate.lower()
    hay = _hay(p, obj, text)

    category = "其他"
    for cat, kws in _CATEGORY_RULES:
        if any(k in hay for k in kws):
            category = cat
            break

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
    cat, sens = classify(fact.predicate, fact.object, fact.text)
    fact.category = cat
    # 绝不把用户已标记的敏感标志降级
    fact.sensitive = bool(getattr(fact, "sensitive", False) or sens)
