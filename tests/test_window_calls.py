"""Window contracts and budgets exercised through ontology and merge entry points."""
from dataclasses import replace
import json

import pytest

from mnemotrace import Config
from mnemotrace.consolidate.adjudication import plan_merge
from mnemotrace.consolidate.ontology import builtin, fits, resolve
from mnemotrace.extraction import ExtractionResponseError, ExtractionTruncatedError
from mnemotrace.llm import Completion, FakeLLM
from mnemotrace.types import Entity, Fact, RelationType
from tests.postgres.ontology_fixtures import assertion, ontology_reply


class Workset:
    def __init__(self, entities=(), relations=()):
        self.nodes = {item.id: item for item in entities}
        self.edges = {item.id: item for item in relations}

    def entities(self, names, limit):
        found = [(item, [f'Identity evidence for {item.name}'], [item.name])
                 for item in self.nodes.values() if item.name in names]
        return found[:limit], len(found) <= limit

    def relations(self, predicate, meaning, left, right, limit):
        return ([item for item in self.edges.values() if fits(item, left, right)
                 or right and fits(item, right, left)][:limit],
                any(item.predicate == predicate for item in self.edges.values()))

    def entity_type(self, code):
        return None

    def has_entity(self, key):
        return key in self.nodes

    def has_relation(self, key):
        return key in self.edges


class BudgetLLM(FakeLLM):
    def complete_result(self, prompt, **kwargs):
        result = super().complete_result(prompt, **kwargs)
        self.calls[-1]['max_tokens'] = kwargs['max_tokens']
        return result


def entry(index, name='Orbit', *, subject='user', predicate='works_at', subject_type='person',
          object_type='organization', text=None, sources=None):
    text = text or f'{subject} {predicate} {name}'
    fact = Fact(subject, predicate, name, text=text, id=f'f{index}', valid_at=10, created_at=100,
                provenance=[f's{index}'])
    hints = assertion(subject, predicate, name, subject_type=subject_type, object_type=object_type)['ontology']
    return fact, hints, sources or {f's{index}': text}


def ontology_model(relations=None):
    return BudgetLLM(handler=lambda prompt, system: ontology_reply(
        prompt, system, relations or {'works_at': ('works_at', False)}))


def compare_model(decide=lambda item, facts: 'coexists'):
    def respond(prompt, system):
        data = json.loads(prompt)
        return json.dumps({'decisions': [dict(id=item['id'], decision=decide(item, data['facts']), reason='fixture')
                                         for item in reversed(data['items'])]})
    return BudgetLLM(handler=respond)


def claim(key, text=None, *, at=10, obj='Orbit'):
    return Fact('user', 'works_at', obj, text=text or key, id=key, valid_at=at, created_at=100,
                subject_id='owner', object_id=obj, relation_id='works_at', provenance=[key + '-source'])


def merge(new, old=(), *, model=None, config=None, **kwargs):
    return plan_merge(new, old, model or compare_model(), config or Config(), None, 100,
                      semantic=True, **kwargs)


def test_empty_window_never_calls_model():
    model = ontology_model()
    assert not resolve(Workset(), [], 'default', 'space', model, Config()).facts
    assert merge([], model=model)[2]['model_calls'] == 0
    assert model.calls == []


def test_more_than_eight_entities_and_relations_use_one_call_per_stage():
    nodes = [Entity(f'Org {i}', 'organization', id=f'existing{i}') for i in range(10)]
    entries = [entry(i, node.name) for i, node in enumerate(nodes)]
    model = ontology_model()
    result = resolve(Workset(nodes), entries, 'default', 'space', model, Config(joint_max_output_tokens=20000))
    assert len(model.calls) == 2
    assert [fact.object_id for fact in result.facts] == [node.id for node in nodes]
    assert all(fact.relation_id == builtin('default', 'works_at').id for fact in result.facts)
    assert all(len(json.loads(call['prompt'])['items']) == 10 for call in model.calls)


def test_window_shares_source_assertions_and_candidate_definitions():
    node = Entity('Orbit', 'organization', id='existing')
    text = 'user works_at Orbit and user likes Orbit'
    entries = [entry(i, predicate=predicate, sources={'source': text})
               for i, predicate in enumerate(('works_at', 'likes'))]
    model = ontology_model({'works_at': ('works_at', False), 'likes': ('likes', False)})
    resolve(Workset([node]), entries, 'default', 'space', model, Config())
    data = json.loads(model.calls[0]['prompt'])
    assert data['sources'] == {'source': text}
    assert len(data['assertions']) == 2
    assert sum(item.get('name') == 'Orbit' and 'proposal' not in item
               for item in data['candidates'].values()) == 1


@pytest.mark.parametrize('max_output,expected_calls', [(8192, 2), (1000, 3)])
def test_same_window_proposals_are_reused_across_budget_splits(max_output, expected_calls):
    entries = [entry(i, predicate='volunteers_at') for i in range(3)]
    model = ontology_model({'volunteers_at': ('volunteers_at', False)})
    output = resolve(Workset(), entries, 'default', 'space', model, Config(joint_max_output_tokens=max_output))
    assert len({fact.object_id for fact in output.facts}) == 1
    assert None not in {fact.relation_id for fact in output.facts}
    assert len({fact.relation_id for fact in output.facts}) == 1
    assert len([item for item in output.records if isinstance(item, RelationType)]) == 1
    assert len(model.calls) == expected_calls


def test_homonyms_remain_distinct_when_model_selects_new():
    node = Entity('Alex', 'organization', id='existing')
    def respond(prompt, system):
        data = json.loads(prompt)
        if 'ENTITY' in system:
            return json.dumps({'decisions': [dict(id=item['id'], decision='new', target=None, evidence='Alex')
                                             for item in data['items']]})
        return ontology_reply(prompt, system, {'works_at': ('works_at', False)})
    output = resolve(Workset([node]), [entry(0, 'Alex'), entry(1, 'Alex')], 'default', 'space',
                     FakeLLM(handler=respond), Config())
    assert len({fact.object_id for fact in output.facts}) == 2
    assert all(fact.object_id != node.id for fact in output.facts)


def test_reversed_proposal_reuse_composes_orientation():
    entries = [entry(0, 'user', subject='Orbit', predicate='employs', subject_type='organization', object_type='person'),
               entry(1, 'Orbit', predicate='works_for')]
    def respond(prompt, system):
        data = json.loads(prompt)
        if 'ENTITY' in system:
            return ontology_reply(prompt, system, {})
        rows = []
        for item in data['items']:
            target = next((key for key in item['candidates'] if key.startswith('r')), None)
            if target is None:
                target = next(key for key in item['candidates'] if data['candidates'][key]['predicate'] == 'works_at')
            rows.append(dict(id=item['id'], decision='reuse', target=target, reverse=True))
        return json.dumps(dict(decisions=rows))
    output = resolve(Workset(), entries, 'default', 'space', FakeLLM(handler=respond), Config())
    assert all(fact.subject == 'user' and fact.object == 'Orbit' and fact.predicate == 'works_at'
               for fact in output.facts)


@pytest.mark.parametrize('response', ['{}', '{"decisions":[]}', '{"decisions":[{"id":"foreign"}]}',
                                    '{"decisions":[{"id":"r0"},{"id":"r0"}]}', 'not json'])
def test_bad_batch_envelope_is_rejected(response):
    with pytest.raises(ExtractionResponseError):
        resolve(Workset(), [entry(0)], 'default', 'space', FakeLLM(responses=[response]), Config())


def test_truncated_output_is_failure_not_unknown():
    with pytest.raises(ExtractionTruncatedError):
        resolve(Workset(), [entry(0)], 'default', 'space',
                FakeLLM(responses=[Completion('{"decisions":[]}', 'length')]), Config())


@pytest.mark.parametrize('reason', [None, '', ' ' * 4, 'x' * 129])
def test_comparison_requires_a_short_reason(reason):
    row = dict(id='0', decision='coexists')
    if reason is not None:
        row['reason'] = reason
    with pytest.raises(ExtractionResponseError):
        merge([claim('new')], [claim('old')], model=FakeLLM(responses=[json.dumps(dict(decisions=[row]))]))


def test_forward_proposal_reference_is_rejected():
    def respond(prompt, system):
        data = json.loads(prompt)
        return json.dumps(dict(decisions=[dict(id=item['id'], decision='reuse', target='r1', reverse=False)
                                         for item in data['items']]))
    with pytest.raises(ExtractionResponseError):
        resolve(Workset(), [entry(0, 'Orbit'), entry(1, 'Nova')], 'default', 'space', FakeLLM(handler=respond), Config())


def test_oversized_ontology_evidence_preserves_text_without_publishing_graph():
    node = Entity('Orbit', 'organization', id='existing')
    fact, hints, _ = entry(0)
    model = ontology_model()
    result = resolve(Workset([node]), [(fact, hints, {'source': 'Orbit' + 'x' * 20000})],
                     'default', 'space', model, Config())
    assert result.facts[0].text == fact.text and result.facts[0].relation_id is None
    assert not result.records and not model.calls


def test_more_than_sixteen_pairs_fit_one_request_and_share_facts():
    old = [claim(f'old{i}', obj=f'Org{i}') for i in range(20)]
    model = compare_model()
    changed, conflicts, stats = merge([claim('new', at=20)], old, model=model,
                                     config=Config(joint_max_output_tokens=20000))
    assert stats['model_calls'] == 1 and not conflicts and len(changed) == 1
    request = json.loads(model.calls[0]['prompt'])
    assert len(request['items']) == 20 and len(request['facts']) == 21


@pytest.mark.parametrize('config', [Config(joint_max_output_tokens=1400),
                                   Config(extraction_max_input_bytes=1800),
                                   Config(joint_context_tokens=2500)])
def test_only_budgets_split_requests_without_dropping_comparisons(config):
    model = compare_model()
    _, conflicts, _ = merge([claim('new', at=20)], [claim(f'old{i}', obj=f'Org{i}') for i in range(5)],
                            model=model, config=config)
    assert not conflicts and len(model.calls) > 1
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
    changed, conflicts, stats = merge([new, third], [old], model=model, duplicate_ids=aliases)
    by_id = {fact.id: fact for fact in changed}
    assert stats['model_calls'] == 1 and stats['duplicates'] == 1 and not conflicts
    assert aliases == {'new': 'old'} and 'new' not in by_id
    assert by_id['old'].provenance == ['old-source', 'new-source'] and by_id['old'].invalid_at == 20
    assert by_id['third'].supersedes == ['old']
    request = json.loads(model.calls[0]['prompt'])
    assert len(request['items']) == 3


def test_inconsistent_duplicate_comparisons_hold_candidates_and_preserve_old():
    old, new, third = claim('old'), claim('new'), claim('third', at=20, obj='Other')
    def decide(item, facts):
        ids = {item['first'], item['second']}
        return 'duplicate' if ids == {'old', 'new'} else 'conflict' if 'old' in ids else 'coexists'
    changed, conflicts, stats = merge([new, third], [old], model=compare_model(decide))
    assert stats['duplicates'] == stats['invalidated'] == 0
    assert {fact.id for fact in changed} == {'new', 'third'}
    assert all(fact.review_pending for fact in changed) and len(conflicts) == 3
    assert old.invalid_at is None and old.provenance == ['old-source']


def test_duplicate_chains_do_not_imply_transitive_equivalence():
    facts = [claim(key) for key in ('a', 'b', 'c')]
    def decide(item, _):
        return 'coexists' if {item['first'], item['second']} == {'a', 'c'} else 'duplicate'
    changed, conflicts, stats = merge(facts[1:], facts[:1], model=compare_model(decide))
    assert stats['duplicates'] == 0 and len(conflicts) == 3
    assert all(fact.review_pending for fact in changed)


@pytest.mark.parametrize('prior', [True, False])
def test_unknown_uses_pending_review_without_changing_published_facts(prior):
    first, second = claim('a'), claim('b', at=20)
    changed, conflicts, stats = merge([second] if prior else [first, second], [first] if prior else [],
                                     model=compare_model(lambda *_: 'unknown'))
    assert len(conflicts) == 1 and stats['invalidated'] == 0
    assert all(fact.review_pending for fact in changed)
    assert first.invalid_at is None and not first.review_pending


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
        return json.dumps({'decisions': [dict(id=item['id'], decision='conflict', reason='Synthetic comparison')
                                         for item in json.loads(prompt)['items']]})
    with pytest.raises(ExtractionResponseError):
        merge([claim('new', at=20)], old, model=FakeLLM(handler=respond),
              config=Config(joint_max_output_tokens=1400))
    assert calls == 2 and all(fact.invalid_at is None for fact in old)


def test_literal_and_symmetric_relations_keep_their_existing_semantics():
    item = entry(0, 'engineer', predicate='occupation', object_type=None)
    result = resolve(Workset(), [item], 'default', 'space',
                     ontology_model({'occupation': ('occupation', False)}), Config())
    assert result.facts[0].object_id is None and result.facts[0].relation_id == builtin('default', 'occupation').id
    assert builtin('default', 'colleague').direction == 'symmetric'


def test_stored_ids_cannot_shadow_local_proposals():
    entity = Entity('Orbit', 'organization', id='e1')
    relation = RelationType('volunteers_at', 'Volunteers for an organization', id='r0',
                            endpoint_types=[['person', 'organization']])
    model = ontology_model({'volunteers_at': ('volunteers_at', False)})
    result = resolve(Workset([entity], [relation]), [entry(i, predicate='volunteers_at') for i in range(2)],
                     'default', 'space', model, Config())
    assert all(fact.object_id == 'e1' and fact.relation_id == 'r0' for fact in result.facts)


def test_relation_proposals_are_reused_after_output_budget_split():
    entries = [entry(i, f'Org {i}', predicate='volunteers_at') for i in range(4)]
    model = ontology_model({'volunteers_at': ('volunteers_at', False)})
    result = resolve(Workset(), entries, 'default', 'space', model, Config(joint_max_output_tokens=350))
    assert len(model.calls) > 1
    assert all(call['system'].startswith('MNEMOTRACE_RELATION_RESOLUTION') for call in model.calls)
    assert len({fact.relation_id for fact in result.facts}) == 1 and result.facts[0].relation_id
    assert any(candidate.get('resolved') for call in model.calls[1:]
               for candidate in json.loads(call['prompt'])['candidates'].values())


def test_reuse_cannot_resolve_an_unknown_earlier_proposal():
    def respond(prompt, system):
        if system.startswith('MNEMOTRACE_ENTITY_RESOLUTION'):
            first, second = json.loads(prompt)['items']
            return json.dumps(dict(decisions=[
                dict(id=first['id'], decision='unknown', target=None, evidence=None),
                dict(id=second['id'], decision='reuse', target=first['id'], evidence='Orbit')]))
        return ontology_reply(prompt, system, {'works_at': ('works_at', False)})

    result = resolve(Workset(), [entry(i) for i in range(3)], 'default', 'space', FakeLLM(handler=respond), Config())
    assert result.facts[0].relation_id is not None
    assert all(fact.relation_id is None for fact in result.facts[1:])


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
        resolve(Workset(), [entry(0)], 'default', 'space', model, Config())
    assert metrics.snapshot()['ops']['pipeline_model_call']['n'] == 1
    assert metrics.snapshot()['counts']['pipeline_usage_unreported'] == 1
