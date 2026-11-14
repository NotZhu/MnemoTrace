"""Window contracts and budgets exercised through ontology and merge entry points."""
from dataclasses import replace
import json

import pytest

from mnemotrace import Config
from mnemotrace.consolidate.adjudication import plan_merge
from mnemotrace.consolidate.ontology import ENTITY_TYPES, builtin, resolve
from mnemotrace.extraction import ExtractionResponseError, ExtractionTruncatedError
from mnemotrace.llm import Completion, FakeLLM
from mnemotrace.types import Entity, Fact, RelationType
from tests.postgres.ontology_fixtures import assertion, ontology_reply


class Workset:
    def __init__(self, entities=(), relations=()):
        self.nodes = {item.id: item for item in entities}
        self.edges = {item.id: item for item in relations}

    def entities(self, names):
        return {item.name.lower(): item for item in self.nodes.values() if item.name.lower() in names}

    def relations(self, meaning, left, right, limit):
        return {item.id: (item, None) for item in self.edges.values()}

    def types(self):
        return {}

    def relation_named(self, predicate):
        return next(((item, None) for item in self.edges.values() if item.predicate == predicate), None)


class BudgetLLM(FakeLLM):
    def complete_result(self, prompt, **kwargs):
        result = super().complete_result(prompt, **kwargs)
        self.calls[-1]['max_tokens'] = kwargs['max_tokens']
        return result


def entry(index, name='Orbit', *, subject='我', predicate='works_at', subject_type='person',
          object_type='organization', text=None, sources=None):
    text = text or f'{subject} {predicate} {name}'
    item = assertion(subject, predicate, name, subject_type=subject_type, object_type=object_type)
    item.pop('predicate')
    sources = sources or {f's{index}': text}
    item.update(text=text, source_ids=list(sources))
    return index, item, {key: dict(content=value, representation='raw') for key, value in sources.items()}


def bind(work, entries, model, config=None):
    return resolve(work, entries, ENTITY_TYPES, 'default', 'space', model, config or Config())


def ontology_model(relations=None):
    return BudgetLLM(handler=lambda prompt, system: ontology_reply(
        prompt, system, relations or {'works_at': ('works_at', False)}))


def compare_model(decide=lambda item, facts: 'coexists'):
    def respond(prompt, system):
        data = json.loads(prompt)
        return json.dumps({'decisions': [dict(id=item['id'], decision=decide(item, data['facts']))
                                         for item in reversed(data['items'])]})
    return BudgetLLM(handler=respond)


def claim(key, text=None, *, at=10, obj='Orbit'):
    return Fact('我', 'works_at', obj, text=text or key, id=key, valid_at=at, created_at=100,
                subject_id='owner', object_id=obj, relation_id='works_at', provenance=[key + '-source'])


def merge(new, old=(), *, model=None, config=None, **kwargs):
    return plan_merge(new, old, model or compare_model(), config or Config(), None, 100,
                      semantic=True, **kwargs)


def test_empty_window_never_calls_model():
    model = ontology_model()
    assert not bind(Workset(), [], model).bindings
    assert merge([], model=model)[1]['model_calls'] == 0
    assert model.calls == []


def test_exact_names_and_more_than_eight_relations_fit_one_request():
    nodes = [Entity(f'Org {i}', 'organization', id=f'existing{i}') for i in range(10)]
    entries = [entry(i, node.name.upper()) for i, node in enumerate(nodes)]
    model = ontology_model()
    result = bind(Workset(nodes), entries, model)
    assert len(model.calls) == 1
    assert [fact['object_id'] for fact in result.bindings.values()] == [node.id for node in nodes]
    assert all(fact['relation_id'] == builtin('default', 'works_at').id for fact in result.bindings.values())


def test_relation_request_shares_definitions_and_keeps_only_fact_text():
    text = 'user works at Orbit and user volunteers at Orbit'
    entries = [entry(i, predicate=predicate, sources={'source': text})
               for i, predicate in enumerate(('works_at', 'volunteers_at'))]
    model = ontology_model()
    bind(Workset(), entries, model)
    data = json.loads(model.calls[0]['prompt'])
    assert set(data) == {'items', 'candidates'}
    assert text not in model.calls[0]['prompt']
    assert [item['text'] for item in data['items']] == [candidate['text'] for _, candidate, _ in entries]
    assert sum(item.get('predicate') == 'works_at' for item in data['candidates'].values()) == 1


def test_large_relation_window_reuses_one_definition_without_carrying_previous_evidence():
    names = [f'Company {i:02}' for i in range(50)]
    source = ' '.join(names) + ' Background detail.' * 30
    entries = [entry(i, name, sources={'shared': source}) for i, name in enumerate(names)]
    config = Config()
    model = ontology_model()
    output = bind(Workset(), entries, model, config)
    assert len(model.calls) > 1
    assert set(output.bindings) == set(range(len(entries)))
    for call in model.calls:
        assert len((call['prompt'] + call['system']).encode('utf-8')) <= config.extraction_max_input_bytes
        data = json.loads(call['prompt'])
        assert sum(candidate['predicate'] == 'works_at' for candidate in data['candidates'].values()) == 1
        assert source not in call['prompt']
        available = set(data['candidates'])
        for item in data['items']:
            assert set(item['candidates']) <= available
            available.add(item['id'])


@pytest.mark.parametrize('max_output,expected_calls', [(8192, 1), (350, 3)])
def test_same_window_proposals_are_reused_across_budget_splits(max_output, expected_calls):
    entries = [entry(i, predicate='volunteers_at') for i in range(3)]
    model = ontology_model()
    output = bind(Workset(), entries, model, Config(joint_max_output_tokens=max_output))
    assert len({fact['object_id'] for fact in output.bindings.values()}) == 1
    assert len({fact['relation_id'] for fact in output.bindings.values()}) == 1
    assert len([value for value, _ in output.records(range(3)) if isinstance(value, RelationType)]) == 1
    assert len(model.calls) == expected_calls
    if expected_calls > 1:
        for call in model.calls[1:]:
            data = json.loads(call['prompt'])
            assert sum(row['predicate'] == 'volunteers_at' for row in data['candidates'].values()) == 1


@pytest.mark.parametrize('max_output', [8192, 350])
def test_reversed_proposal_reuse_composes_orientation(max_output):
    entries = [entry(0, 'Alice', subject='Orbit', predicate='employs', subject_type='organization', object_type='person'),
               entry(1, 'Orbit', subject='Alice', predicate='works_for')]
    def respond(prompt, system):
        data = json.loads(prompt)
        rows = []
        for item in data['items']:
            target = next((key for key in item['candidates'] if key == 'r0'), None)
            if target is None:
                target = next(key for key in item['candidates'] if data['candidates'][key]['predicate'] == 'works_at')
            rows.append(dict(id=item['id'], decision='reuse', target=target,
                             reverse=target == 'r0' or item['id'] == 'r0', predicate=None))
        return json.dumps(dict(decisions=rows))
    output = bind(Workset(), entries, FakeLLM(handler=respond), Config(joint_max_output_tokens=max_output))
    assert len(output.bindings) == 2
    assert all((f['subject'], f['object'], f['predicate']) == ('Alice', 'Orbit', 'works_at')
               for f in output.bindings.values())


@pytest.mark.parametrize('response', ['{}', '{"decisions":[]}', '{"decisions":[{"id":"foreign"}]}',
                                    '{"decisions":[{"id":"r0"},{"id":"r0"}]}', 'not json'])
def test_bad_batch_envelope_is_rejected(response):
    with pytest.raises(ExtractionResponseError):
        bind(Workset(), [entry(0)], FakeLLM(responses=[response]))


def test_truncated_output_is_failure_not_unknown():
    with pytest.raises(ExtractionTruncatedError):
        bind(Workset(), [entry(0)], FakeLLM(responses=[Completion('{"decisions":[]}', 'length')]))


@pytest.mark.parametrize('fields', [{'decision': 'coexists', 'reason': 'extra'}, {},
                                    {'decision': ['coexists']}, {'decision': 'invalid'}])
def test_comparison_accepts_only_allowed_decisions(fields):
    row = dict(id='0', **fields)
    with pytest.raises(ExtractionResponseError):
        merge([claim('new')], [claim('old')], model=FakeLLM(responses=[json.dumps(dict(decisions=[row]))]))


def test_forward_proposal_reference_is_rejected():
    def respond(prompt, system):
        data = json.loads(prompt)
        return json.dumps(dict(decisions=[dict(id=item['id'], decision='reuse', target='r1',
                                              reverse=False, predicate=None) for item in data['items']]))
    with pytest.raises(ExtractionResponseError):
        bind(Workset(), [entry(0, 'Orbit'), entry(1, 'Nova')], FakeLLM(handler=respond))


def test_oversized_fact_sentence_never_produces_a_fact():
    model = ontology_model()
    result = bind(Workset(), [entry(0, text='Orbit ' + 'x' * 20000)], model)
    assert not result.bindings and not result.records([]) and not model.calls


def test_large_raw_evidence_is_only_used_for_entity_binding():
    model = ontology_model()
    result = bind(Workset(), [entry(0, sources={'source': 'Orbit. ' + 'x' * 20000})], model)
    assert len(result.bindings) == len(model.calls) == 1


def test_more_than_sixteen_pairs_fit_one_request_and_share_facts():
    old = [claim(f'old{i}', obj=f'Org{i}') for i in range(20)]
    model = compare_model()
    changed, stats = merge([claim('new', at=20)], old, model=model,
                                     config=Config(joint_max_output_tokens=20000))
    assert stats['model_calls'] == 1 and len(changed) == 1
    request = json.loads(model.calls[0]['prompt'])
    assert len(request['items']) == 20 and len(request['facts']) == 21


@pytest.mark.parametrize('config', [Config(joint_max_output_tokens=120),
                                   Config(extraction_max_input_bytes=1350),
                                   Config(joint_context_tokens=1450)])
def test_only_budgets_split_requests_without_dropping_comparisons(config):
    model = compare_model()
    _, _ = merge([claim('new', at=20)], [claim(f'old{i}', obj=f'Org{i}') for i in range(5)],
                            model=model, config=config)
    assert len(model.calls) > 1
    items = [item['id'] for call in model.calls for item in json.loads(call['prompt'])['items']]
    assert len(items) == len(set(items)) == 5
    for call in model.calls:
        size = len((call['prompt'] + call['system']).encode())
        assert size <= config.extraction_max_input_bytes
        assert call['max_tokens'] <= config.joint_max_output_tokens
        if config.joint_context_tokens:
            assert size + call['max_tokens'] <= config.joint_context_tokens


def test_duplicate_and_third_fact_conflict_are_compared_once_together():
    old, new, third = claim('old'), claim('new'), claim('third', at=20, obj='Other')
    model = compare_model(lambda item, facts: 'duplicate' if 'duplicate' in item['allowed'] else 'conflict')
    aliases = {}
    changed, stats = merge([new, third], [old], model=model, duplicate_ids=aliases)
    by_id = {fact.id: fact for fact in changed}
    assert stats['model_calls'] == 1 and stats['duplicates'] == 1
    assert aliases == {'new': 'old'} and 'new' not in by_id
    assert by_id['old'].provenance == ['old-source', 'new-source'] and by_id['old'].invalid_at == 20
    assert by_id['third'].supersedes == ['old']
    request = json.loads(model.calls[0]['prompt'])
    assert len(request['items']) == 3


def test_inconsistent_duplicate_comparisons_discard_candidates_and_preserve_old():
    old, new, third = claim('old'), claim('new'), claim('third', at=20, obj='Other')
    def decide(item, facts):
        ids = {item['first'], item['second']}
        return 'duplicate' if ids == {'old', 'new'} else 'conflict' if 'old' in ids else 'coexists'
    changed, stats = merge([new, third], [old], model=compare_model(decide))
    assert stats['duplicates'] == stats['invalidated'] == 0
    assert not changed
    assert old.invalid_at is None and old.provenance == ['old-source']


def test_duplicate_chains_do_not_imply_transitive_equivalence():
    facts = [claim(key) for key in ('a', 'b', 'c')]
    def decide(item, _):
        return 'coexists' if {item['first'], item['second']} == {'a', 'c'} else 'duplicate'
    changed, stats = merge(facts[1:], facts[:1], model=compare_model(decide))
    assert stats['duplicates'] == 0 and not changed


@pytest.mark.parametrize('prior', [True, False])
def test_unknown_discards_candidate_without_changing_published_facts(prior):
    first, second = claim('a'), claim('b', at=20)
    changed, stats = merge([second] if prior else [first, second], [first] if prior else [],
                                     model=compare_model(lambda *_: 'unknown'))
    assert not changed and stats['invalidated'] == 0
    assert first.invalid_at is None


@pytest.mark.parametrize('field', ['valid_at', 'object', 'text'])
def test_duplicate_qualification_constraints_are_not_relaxed(field):
    first, second = claim('a', text='Owns 1 widget'), claim('b', text='Has 1 widget')
    second = replace(second, **{field: {'valid_at': 20, 'object': 'Other', 'text': 'Has 2 widgets'}[field]},
                     **({'object_id': 'Other'} if field == 'object' else {}))
    model = compare_model(lambda *_: 'duplicate')
    with pytest.raises(ExtractionResponseError):
        merge([second], [first], model=model)


def test_later_batch_failure_never_mutates_supplied_timeline():
    old = [claim(f'old{i}', obj=f'Org{i}') for i in range(4)]
    calls = 0
    def respond(prompt, system):
        nonlocal calls
        calls += 1
        if calls == 2:
            return '{"decisions":[]}'
        return json.dumps({'decisions': [dict(id=item['id'], decision='conflict')
                                         for item in json.loads(prompt)['items']]})
    with pytest.raises(ExtractionResponseError):
        merge([claim('new', at=20)], old, model=FakeLLM(handler=respond),
              config=Config(joint_max_output_tokens=120))
    assert calls == 2 and all(fact.invalid_at is None for fact in old)


def test_literal_relations_keep_their_existing_semantics():
    item = entry(0, 'engineer', predicate='occupation', object_type=None)
    result = bind(Workset(), [item], ontology_model())
    assert result.bindings[0]['object_id'] is None
    assert result.bindings[0]['relation_id'] == builtin('default', 'occupation').id


def test_reuse_cannot_resolve_an_unknown_earlier_proposal():
    def respond(prompt, system):
        first, second = json.loads(prompt)['items']
        return json.dumps(dict(decisions=[
            dict(id=first['id'], decision='unknown', target=None, reverse=False, predicate=None),
            dict(id=second['id'], decision='reuse', target=first['id'], reverse=False, predicate=None)]))
    with pytest.raises(ExtractionResponseError):
        bind(Workset(), [entry(i) for i in range(2)], FakeLLM(handler=respond))


def test_meter_preserves_provider_identity_and_counts_failed_attempts(monkeypatch):
    import sys
    from types import SimpleNamespace
    from mnemotrace.consolidate.pipeline import config_identity
    from mnemotrace.llm.execution import ModelMeter, one_attempt
    from mnemotrace.llm.litellm_llm import LiteLLMClient
    from mnemotrace.metrics import Metrics

    monkeypatch.setitem(sys.modules, 'litellm', SimpleNamespace())
    provider = LiteLLMClient('synthetic', max_attempts=3)
    metrics = Metrics()
    wrapped = ModelMeter(provider, metrics)
    config = SimpleNamespace(config=Config(), policy={}, llm=provider, embedder=None)
    expected = config_identity(config)
    config.llm = wrapped
    assert config_identity(config) == expected
    bounded = one_attempt(wrapped)
    assert bounded.inner.max_attempts == 1 and provider.max_attempts == 3

    def fail(*_):
        raise RuntimeError('synthetic provider failure')

    model = ModelMeter(FakeLLM(handler=fail), metrics)
    with pytest.raises(RuntimeError):
        bind(Workset(), [entry(0)], model)
    assert metrics.snapshot()['ops']['pipeline_model_call']['n'] == 1
    assert metrics.snapshot()['counts']['pipeline_usage_unreported'] == 1
