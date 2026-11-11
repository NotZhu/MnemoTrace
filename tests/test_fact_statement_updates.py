"""Explicit fact sentences and structured assertions are replaced atomically."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from mnemotrace.types import Fact
from tests.model_fixtures import MemoryEngine


def test_structured_attribute_edit_preserves_values_graph_and_provenance():
    memory = MemoryEngine()
    memory.add('I live in Beijing.', user_id='synthetic_user', session_id='synthetic_source')
    memory.consolidate()
    fact, = memory._all_facts()
    provenance, slot = list(fact.provenance), fact.slot
    sentence = '演示用户住在上海，工作日通勤。'
    memory.update_fact(fact.id, subject=fact.subject, predicate='lives_in', object='Shanghai',
                       text=sentence, object_kind='entity')
    assert fact.text == sentence and fact.object == 'Shanghai'
    assert fact.slot == slot and fact.source == 'user' and fact.provenance == provenance
    assert fact.embedding == memory.embedder.embed(sentence)
    names = {node['name'] for node in memory.graph_data('synthetic_user')['nodes']}
    assert 'Shanghai' in names and 'Beijing' not in names and sentence not in names
    assert 'display' not in asdict(fact)


@pytest.mark.parametrize('patch', [
    {'text': '新事实'}, {'object': '上海'}, {'subject': 'user', 'predicate': 'likes', 'object': '茶'},
    {'subject': 'user', 'predicate': 'likes', 'object': '茶', 'text': ' \n\t'},
])
def test_incomplete_or_blank_content_patch_has_no_side_effects(patch):
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。')
    before, graph = deepcopy(asdict(fact)), deepcopy(memory.graph_data('default'))
    with pytest.raises(ValueError, match='subject, predicate, object and text'):
        memory.update_fact(fact.id, **patch)
    assert asdict(fact) == before and memory.graph_data('default') == graph


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


def test_unchanged_complete_content_preserves_history_and_source():
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。', valid_at=1)
    fact.invalid_at = fact.expired_at = 2
    fact.source = 'extracted'
    before = deepcopy(asdict(fact))
    memory.update_fact(fact.id, **{key: getattr(fact, key) for key in ('subject', 'predicate', 'object', 'text')})
    assert asdict(fact) == before and not fact.is_live()


def test_fact_edit_keeps_existing_state_on_embedding_failure(monkeypatch):
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。')
    before, graph = deepcopy(asdict(fact)), deepcopy(memory.graph_data('default'))

    def fail(text):
        raise RuntimeError('Synthetic embedding failure')

    monkeypatch.setattr(memory.embedder, 'embed', fail)
    with pytest.raises(RuntimeError, match='Synthetic embedding failure'):
        memory.update_fact(fact.id, subject='user', predicate='likes', object='绘画', text='用户喜欢绘画。')
    assert asdict(fact) == before and memory.graph_data('default') == graph


def test_structured_only_change_reuses_sentence_vector(monkeypatch):
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。')
    vector = list(fact.embedding)
    monkeypatch.setattr(memory.embedder, 'embed', lambda text: pytest.fail('Unchanged text was embedded'))
    memory.update_fact(fact.id, subject='user', predicate='prefers', object='阅读', text=fact.text)
    assert fact.predicate == 'prefers' and fact.embedding == vector


def test_retired_statement_parameter_is_rejected():
    memory = MemoryEngine()
    fact = memory.add_fact('user', 'likes', '阅读', text='用户喜欢阅读。')
    with pytest.raises(TypeError, match='statement'):
        memory.update_fact(fact.id, statement='旧输入')
