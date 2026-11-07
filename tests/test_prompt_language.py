"""默认提示词语言与模型调用边界；使用合成回复，不依赖外部模型。"""

from tests.model_fixtures import MemoryEngine


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
    from mnemotrace import MemoryEngine as NativeMemory
    model = FakeLLM(handler=respond)
    mem = NativeMemory(llm=model)
    mem.set_policy(extract_instruction='保留饮食偏好')
    ep = mem.add('I like black coffee.')
    mem.consolidate([ep])
    assert ep.content in model.calls[0]['prompt']
    assert '英文 snake_case' in model.calls[0]['system']
    assert '保留饮食偏好' in model.calls[0]['system']


def test_chinese_preference_table_preserves_values_and_budget_classification():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "likes", "black coffee", user_id="synthetic-user")
    block = mem._preference_block("synthetic-user", "What do I like?", [fact])
    assert block.startswith("当前偏好记录（结构化）：\n日期 | 人物或对象 | 偏好关系 | 具体内容\n")
    assert "user | likes | black coffee" in block
    assert fact.predicate == "likes"
    assert mem._evidence_block_title(block) == "当前偏好记录（结构化）"
    assert mem._evidence_block_kind(block) == "preference"
    assert mem._evidence_block_kind("PREFERENCE RECORDS (current, structured):\nlegacy") == "other"
    assert len(mem._fit_blocks_by_evidence_budget([block], 100, None)) <= 100
