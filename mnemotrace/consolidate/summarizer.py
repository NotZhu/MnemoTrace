"""分层抽象（AGENTS.md 「架构概览与代码导航」）：L2 会话摘要 + L3 用户画像。

这些是*规模化原语*。与其把每个原始会话塞进读上下文（超出模型窗口就无法扩展——AGENTS.md
Bet A/E），整合过程把每个会话蒸馏成紧凑的 L2 摘要、把整个用户蒸馏成 L3 画像，在关键路径之外
一次算好并存起来。读路径随后只检索一小片摘要 + 画像，让精简上下文以低成本覆盖大量会话。

事件摘要只在达到长度条件且有模型时生成，失败不以摘录冒充成功；原文与确定性画像仍可离线读取。
"""
from __future__ import annotations

from typing import Optional

from ..llm import LLM
from ..types import Fact
from ..util import time_key

# 默认会话摘要系统 prompt
_SESSION_DIGEST_SYSTEM = (
    "将一段带日期的对话压缩为信息密集、忠于事实的摘要，供记忆索引使用。"
    "后续问题应当能够仅依据这份摘要得到回答。\n"
    "使用与原对话相同的主要语言撰写摘要。专有名称、准确数值及需要逐字保留的原文值保持原样。"
    "逐字保留所有具体值，不要泛化或四舍五入，包括准确的数字、数量、价格、时长、日期和时间、"
    "专有名称（人物、地点、品牌、产品、作品名称）及计数。\n"
    "逐项列出用户提及的不同事物，确保后续询问“有多少”时可以依据摘要计数。"
    "记录偏好、决定、事件和计划，只省略无实质内容的表达和闲聊。\n"
    "表达简洁，但具体信息必须完整，尤其不能遗漏数字或名称。直接输出摘要，不要添加开场说明。"
)

# 结构化 4 层画像（源自 Tencent Agent-Memory L3 "deep scan"）：身份锚点 → 兴趣 → 协作方式 → 核心目标
# 按层组织的事实档案，比平铺 fact 堆更能支撑偏好 / 推荐类回答；这是 Hunyuan/腾讯在偏好场景获利的核心来源。
# 默认用户画像系统 prompt
_PERSONA_SYSTEM = (
    "为长期记忆系统维护一份简洁、结构化的用户画像。根据用户的已知事实，使用简体中文归纳，"
    "专有名称及需要准确保留的原文值保持原样。\n"
    "严格使用以下四个中文小节标题，每个标题单独一行并以中文冒号结尾，内容列在其下。"
    "它们均为同一份用户画像的内部小节，不要添加其他顶层标题；没有事实支持的小节直接省略。\n"
    "身份与背景：\n姓名、身份或工作、所在地、重要关系，以及值得记录的所有物或工具。\n"
    "兴趣与偏好：\n爱好、喜欢与不喜欢的事物、习惯，以及偏好的品牌、类型和风格。"
    "如果日期能够体现变化，应区分当前与过去的情况。\n"
    "限制与目标：\n需求、限制、过敏情况、当前计划，以及正在努力实现的目标。\n"
    "回答与协助偏好：\n用户明确表达的、希望获得帮助的方式，以及长期有效的指令。\n"
    "只保留具体信息，例如名称、品牌、地点和数字。所有内容必须有已有事实支持，不得编造。保持简洁。"
)


class ProfileBuilder:
    """将用户事实整理为档案字典或文字画像；无 LLM 时使用规则生成画像。"""

    _KEYS = {
        "works_at", "lives_in", "name", "born_in", "married_to", "occupation", "job_title", "age",
        "diet",
    }

    def build(self, subject: str, live_facts: list[Fact]) -> dict[str, str]:
        # #7 档案权威性：两条存活 fact 共享槽位时，权威的（source="user"）胜出，同一来源层级内
        # 取最近的——手动固定的精确值绝不会被模糊的自动提取值（或遍历顺序）遮蔽。
        best: dict[str, Fact] = {}
        for f in live_facts:
            if f.subject.lower() != subject.lower() or not (
                    f.predicate in self._KEYS or f.predicate.startswith("favorite_")):
                continue
            cur = best.get(f.predicate)
            if cur is None or (f.source == "user", time_key(f.valid_at)) > (cur.source == "user", time_key(cur.valid_at)):
                best[f.predicate] = f
        return {p: f.object for p, f in best.items()}

    def narrative(self, subject: str, live_facts: list[Fact], llm: Optional[LLM] = None,
                  system: Optional[str] = None) -> str:
        """以散文形式呈现 L3 画像。有 LLM 时由存活的 fact 合成；否则把确定性档案 dict + 显著
        fact 渲染成可读文本。`system` 覆盖画像 prompt（可按用户编辑的 policy）。"""
        user_facts = [f for f in live_facts if f.subject.lower() == subject.lower()]
        if not user_facts:
            user_facts = live_facts
        # #7: 权威（固定）的 fact 排最前，其后按显著度——确保它们在 [:12]/[:120] 截断中存活。
        ordered = sorted(user_facts, key=lambda x: (x.source != "user", -x.salience))
        if llm is None:
            prof = self.build(subject, live_facts)
            lines = [f"{k.replace('_', ' ')}: {v}" for k, v in prof.items()]
            for f in ordered[:12]:  # 补几条 top fact 增加细节
                lines.append(f.text)
            return "\n".join(dict.fromkeys(lines))  # 去重，保持顺序
        facts_block = "\n".join(f"- {f.text}" for f in ordered[:120])
        try:
            return self.llm_complete(llm, facts_block, system)
        except Exception:  # noqa: BLE001
            return "\n".join(f.text for f in user_facts[:20])

    @staticmethod
    def llm_complete(llm: LLM, facts_block: str, system: Optional[str] = None) -> str:
        out = llm.complete(f"用户事实：\n{facts_block}\n\n用户画像：", system=system or _PERSONA_SYSTEM)
        return out.strip()


# 公开别名，让 Memory policy 层能把内置 prompt 作为可编辑默认值暴露出去。
SESSION_DIGEST_SYSTEM = _SESSION_DIGEST_SYSTEM
PERSONA_SYSTEM = _PERSONA_SYSTEM
