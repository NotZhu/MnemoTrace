"""Pure session-outcome protocol and display rules; execution belongs to the pipeline."""

# 四种类型直接作为 predicate。这样挑选是为了让人读原始 fact 列表时一眼看出每条在断言什么，
# 检索也能按 kind 过滤而无需改 schema。
OUTCOME_PREDICATES = ("decision", "finding", "lesson", "open_question")

# 标记"这条产出来自会话蒸馏"。它必须在用户编辑后仍然保留：classify() 会从内容重新推导
# category，会把被编辑过的结论丢进通用桶里，在所有按 category 分组的界面上悄悄变回普通 fact。
OUTCOME_CATEGORY = "会话结论"

OUTCOME_SYSTEM = (
    "你在为一个长期记忆系统，从会话记录中提炼值得未来参考的结论。\n"
    "不限制会话场景，根据内容是否包含有原文依据的决定、发现、经验或待解决问题来判断。\n"
    "\n"
    "只记录**下次真正用得上**的东西，分四类：\n"
    "- decision: 做了什么选择、为什么，以及被否决的选项\n"
    "用户明确陈述、会影响后续做法或安排的已定决定，应作为 decision 保留。\n"
    "一句话也可以构成决定，不要求已有执行记录或实验验证；临时要求助手执行的任务不算已定决定。\n"
    "- finding: 验证出来的事实、数字或根因（要带证据）\n"
    "- lesson: 踩过的坑，以及它在什么条件下成立\n"
    "- open_question: 悬而未决、下次要接着处理的\n"
    "\n"
    "普通个人属性不作为会话结论。所有结论必须有原文依据，不推断未表达的选择。\n"
    "不要记录：一次性的任务指令（“先修 bug 1”“不要 push”）、进度播报、寒暄、"
    "工具调用细节、任何密钥或凭据。\n"
    "\n"
    '只输出 JSON 数组，每项 {"kind","statement","why"}。\n'
    "statement 必须能脱离这次会话独立看懂——不要出现“上面那个”“刚才说的”这类指代。\n"
    "statement 使用 15–400 字符的完整陈述，不为了凑长度补造细节。\n"
    "why 一句话，说明原文依据；用户明确陈述的决定也可作为依据，未说明原因时不要编造原因。\n"
    "宁缺毋滥：这次会话没有值得长期记住的东西，就返回 []。"
)

def split_outcome_text(text: str) -> tuple[str, str]:
    """从 outcome fact 的 `text` 中还原 (statement, why)。

    放在这里、就在它所反转的格式下方十行处，是因为两者绝不能各自漂移：分隔符在上方唯一一处写入、
    在这里唯一一处读取。调用方需要拿回两半，是因为 `text` 是嵌入用形态（statement + 依据），而
    阅读者希望主张和依据分开呈现——编辑时也必须重建同样的形状，而不是拍平。
    """
    statement, sep, why = text.partition(" （依据：")
    if not sep:
        return text.strip(), ""
    why = why.strip()
    if why.endswith("）"):
        why = why[:-1]
    return statement.strip(), why.strip()
