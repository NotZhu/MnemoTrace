"""Explicit fact sentences and structured assertions are replaced atomically."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from mnemotrace.types import Fact
from tests.model_fixtures import MemoryEngine


@pytest.mark.parametrize('text', ['', ' \n\t', None, 42])
def test_blank_or_nonstring_sentence_rejected_before_manual_write(text):
    memory = MemoryEngine()
    with pytest.raises(ValueError, match='non-empty'):
        memory.add_fact('user', 'likes', '阅读', text=text)
    assert memory.fact_store.values() == []


def test_missing_sentence_has_no_default_or_fallback():
    with pytest.raises(TypeError, match='text'):
        Fact('user', 'likes', '阅读')
    with pytest.raises(TypeError, match='text'):
        MemoryEngine().add_fact('user', 'likes', '阅读')


def test_sensitivity_does_not_reembed_fact_text(monkeypatch):
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。')
    before = asdict(fact)
    monkeypatch.setattr(memory.embedder, 'embed', lambda text: pytest.fail('Unchanged text was embedded'))
    updated = memory.set_fact_sensitive(fact.id, False)
    assert asdict(updated) == {**before, 'sensitive': False, 'sensitive_override': True}


def test_failed_embedding_does_not_replace_an_existing_fact(monkeypatch):
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。')
    before = deepcopy(asdict(fact))

    def fail(text):
        raise RuntimeError('Synthetic embedding failure')

    monkeypatch.setattr(memory.embedder, 'embed', fail)
    with pytest.raises(RuntimeError, match='Synthetic embedding failure'):
        memory.add_fact('user', 'likes', '绘画', text='用户喜欢绘画。')
    assert [asdict(item) for item in memory.fact_store.values()] == [before]
