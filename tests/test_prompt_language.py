"""默认提示词语言与模型调用边界；使用合成回复，不依赖外部模型。"""

from tests.model_fixtures import Memory


class RecordingLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, prompt, system="", **kwargs):
        self.calls.append((prompt, system))
        return self.response


def test_joint_extraction_uses_chinese_protocol_and_policy():
    from tests.test_pipeline_sources import respond
    from mnemotrace.llm import FakeLLM
    from mnemotrace import Memory as NativeMemory
    model = FakeLLM(handler=respond)
    mem = NativeMemory(llm=model)
    mem.set_policy(extract_instruction='保留饮食偏好')
    ep = mem.add('I like black coffee.')
    mem.consolidate([ep])
    assert ep.content in model.calls[0]['prompt']
    assert '英文 snake_case' in model.calls[0]['system']
    assert '保留饮食偏好' in model.calls[0]['system']


def test_policy_exposes_chinese_defaults_and_preserves_custom_overrides(tmp_path):
    llm = RecordingLLM("回答与协助偏好：\n先给结论。")
    mem = Memory(llm=llm)
    defaults = mem.get_policy()["defaults"]
    assert "对话记忆提取器" in defaults["extract_system"]
    assert "使用与原对话相同的主要语言撰写摘要。" in defaults["summary_system"]
    assert "使用简体中文归纳" in defaults["persona_system"]
    for title in ("身份与背景", "兴趣与偏好", "限制与目标", "回答与协助偏好"):
        assert title + "：" in defaults["persona_system"]

    mem.add_fact("user", "instruction", "先给结论", user_id="synthetic-user")
    assert mem.build_persona("synthetic-user") == llm.response
    assert llm.calls[-1][1] == defaults["persona_system"]
    calls = len(llm.calls)
    mem.build_persona("synthetic-user")
    assert len(llm.calls) == calls

    mem.set_policy(persona_system="CUSTOM PROFILE", summary_system="CUSTOM SUMMARY")
    mem.build_persona("synthetic-user")
    assert llm.calls[-1][1] == "CUSTOM PROFILE"
    assert mem.get_policy()["policy"]["summary_system"] == "CUSTOM SUMMARY"
    snapshot = str(tmp_path / "synthetic-policy.jsonl")
    mem.save(snapshot)
    assert Memory.open(snapshot).get_policy()["policy"]["persona_system"] == "CUSTOM PROFILE"
    mem.set_policy(persona_system="", summary_system="")
    mem.build_persona("synthetic-user")
    assert llm.calls[-1][1] == defaults["persona_system"]
    assert mem._effective("summary_system") == defaults["summary_system"]


def test_chinese_preference_table_preserves_values_and_budget_classification():
    mem = Memory()
    fact = mem.add_fact("user", "likes", "black coffee", user_id="synthetic-user")
    block = mem._preference_block("synthetic-user", "What do I like?", [fact])
    assert block.startswith("当前偏好记录（结构化）：\n日期 | 人物或对象 | 偏好关系 | 具体内容\n")
    assert "user | likes | black coffee" in block
    assert fact.predicate == "likes"
    assert mem._evidence_block_title(block) == "当前偏好记录（结构化）"
    assert mem._evidence_block_kind(block) == "preference"
    assert mem._evidence_block_kind("PREFERENCE RECORDS (current, structured):\nlegacy") == "other"
    assert len(mem._fit_blocks_by_evidence_budget([block], 100, None)) <= 100
