"""默认提示词语言与模型调用边界；使用合成回复，不依赖外部模型。"""
import json

from tests.model_fixtures import Memory
from tests.model_fixtures import LLMExtractor
from mnemotrace.consolidate.summarizer import SessionSummarizer
from mnemotrace.types import Episode


class RecordingLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, prompt, system="", **kwargs):
        self.calls.append((prompt, system))
        return self.response


def test_extraction_uses_chinese_instructions_and_preserves_internal_fields():
    row = {"subject": "user", "predicate": "likes", "object": "black coffee", "text": "I like black coffee."}
    llm = RecordingLLM(json.dumps([row]))
    extractor = LLMExtractor(llm)
    extractor.instruction = "保留饮食偏好"
    episode = Episode(content="I like black coffee.", user_id="synthetic-user")
    facts = extractor.extract(episode)
    assert len(facts) == 1
    assert facts[0].predicate == "likes"
    assert facts[0].object == "black coffee"
    assert facts[0].display == row["text"]
    assert facts[0].provenance == [episode.id]
    prompt, system = llm.calls[0]
    assert episode.content in prompt
    assert "predicate 使用简短的英文 snake_case" in system
    assert "中文对话生成中文句子，英文对话生成英文句子" in system
    assert "保留饮食偏好" in system


def test_summary_requests_source_language_without_modifying_original_content():
    episode = Episode(content="Read Example Book for 45 minutes on 2026-10-16. " * 60)
    llm = RecordingLLM("Read Example Book for 45 minutes on 2026-10-16.")
    summary = SessionSummarizer(llm).summarize(episode)
    prompt, system = llm.calls[0]
    assert "使用与原对话相同的主要语言撰写摘要。" in system
    assert "即使原对话是其他语言" not in system
    assert "专有名称、准确数值" in system
    assert episode.content in prompt
    assert summary == llm.response
    assert episode.content == "Read Example Book for 45 minutes on 2026-10-16. " * 60
    from mnemotrace.consolidate.summaries import SummaryError
    import pytest
    with pytest.raises(SummaryError, match="llm_not_configured"):
        SessionSummarizer().summarize(episode)


def test_policy_exposes_chinese_defaults_and_preserves_custom_overrides(tmp_path):
    llm = RecordingLLM("回答与协助偏好：\n先给结论。")
    mem = Memory(llm=llm)
    defaults = mem.get_policy()["defaults"]
    assert "精确信息提取引擎" in defaults["extract_system"]
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
    assert mem.summarizer.system == "CUSTOM SUMMARY"
    snapshot = str(tmp_path / "synthetic-policy.jsonl")
    mem.save(snapshot)
    assert Memory.open(snapshot).get_policy()["policy"]["persona_system"] == "CUSTOM PROFILE"
    mem.set_policy(persona_system="", summary_system="")
    mem.build_persona("synthetic-user")
    assert llm.calls[-1][1] == defaults["persona_system"]
    assert mem.summarizer.system == defaults["summary_system"]


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
