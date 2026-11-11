"""Retrieval evidence comes from stored fields, without domain synonym tables."""
import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.retrieve.answers import chain_terms, should_abstain
from mnemotrace.types import Fact


def test_chain_terms_contains_only_stored_fields():
    fact = Fact("Ana", "works_at", "Orbit")
    assert chain_terms(fact).split() == f"{fact.subject} works at {fact.object} {fact.text}".split()


@pytest.mark.parametrize("predicate,query", [
    ("favorite_language", "What is my favorite food?"),
    ("preferred_format", "What is my preferred language?"),
    ("primary_transport", "What is my primary email?"),
])
def test_partial_compound_attribute_is_insufficient(predicate, query):
    fact = Fact("user", predicate, "Synthetic value")
    assert should_abstain(query, [fact], [], 0.3)


def test_lexical_and_semantic_evidence_support_arbitrary_predicates():
    fact = Fact("user", "primary_transport", "Synthetic tram")
    assert not should_abstain("What is my primary transport?", [fact], [], 0.3)
    assert not should_abstain("Tell me about the tram", [fact], [], 0.3)
    assert not should_abstain("How do I commute?", [fact], [0.8], 0.3)


def test_subject_only_and_grammar_only_matches_do_not_answer():
    memory = MemoryEngine(config=Config(digest_fallback=False))
    memory.add_fact("Ana", "works_at", "Orbit")
    assert memory.search("What is Ana's preferred language?").abstained
    assert memory.search("What is it?").abstained


def test_digest_fallback_requires_query_evidence():
    from tests.model_fixtures import MemoryEngine as FixtureMemory, seed_digests

    memory = FixtureMemory()
    source = memory.add("Ana's favorite programming language is Python.")
    seed_digests(memory, [source])
    assert memory._digest_fallback("What is Ana's favorite food?", "default") is None
    assert memory._digest_fallback("What is Ana's favorite programming language?", "default") is not None



def test_explicit_answer_distinguishes_empty_configuration_and_failure():
    from mnemotrace.llm.base import Completion
    from mnemotrace.llm.fake import FakeLLM
    from mnemotrace.service import _answer_from_memory

    assert _answer_from_memory(None, "Synthetic question", "Synthetic evidence") == ("", "model_not_configured")
    model = FakeLLM(responses=["", Completion("truncated", "length"), "Synthetic answer"])
    assert _answer_from_memory(model, "Synthetic question", "") == ("", "no_evidence")
    assert model.calls == []
    assert _answer_from_memory(model, "Synthetic question", "Synthetic evidence") == ("", "empty")
    assert _answer_from_memory(model, "Synthetic question", "Synthetic evidence") == ("", "model_failed")
    assert _answer_from_memory(model, "Synthetic question", "Synthetic evidence") == ("Synthetic answer", "answered")
