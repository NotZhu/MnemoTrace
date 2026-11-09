"""默认提示词语言与模型调用边界；使用合成回复，不依赖外部模型。"""


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
