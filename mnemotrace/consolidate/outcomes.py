"""Display rules for facts recorded as decisions, findings, lessons, and open questions."""

# 四种类型直接作为 predicate。这样挑选是为了让人读原始 fact 列表时一眼看出每条在断言什么，
# 检索也能按 kind 过滤而无需改 schema。
OUTCOME_PREDICATES = ("decision", "finding", "lesson", "open_question")

# 标记这条产出来自会话蒸馏。
# category，会把被编辑过的结论丢进通用桶里，在所有按 category 分组的界面上悄悄变回普通 fact。
OUTCOME_CATEGORY = "会话结论"

def split_outcome_text(text: str) -> tuple[str, str]:
    """从 outcome fact 的 `text` 中还原 (statement, why)。

    调用方需要拿回两半，是因为 `text` 是嵌入用形态（statement + 依据），而
    阅读者希望主张和依据分开呈现——编辑时也必须重建同样的形状，而不是拍平。
    """
    statement, sep, why = text.partition(" （依据：")
    if not sep:
        return text.strip(), ""
    why = why.strip()
    if why.endswith("）"):
        why = why[:-1]
    return statement.strip(), why.strip()
