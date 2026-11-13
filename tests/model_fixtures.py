"""Recorded synthetic model outputs for existing pipeline regressions.

These fixtures test consumers of extracted facts, not language understanding.
There is no sentence grammar or semantic inference in this test double.
"""
from __future__ import annotations

import json
from pathlib import Path

from mnemotrace import MemoryEngine as NativeMemory
from tests.embedder_fixtures import MockEmbedder
from mnemotrace.consolidate.planning import WINDOW_PREFIX

RESPONSES = json.loads(Path(__file__).with_name('extraction_responses.json').read_text(encoding='utf-8'))
for _source, _facts in list(RESPONSES.items()):
    if _source.startswith('user: ') and '\n' not in _source:
        RESPONSES.setdefault(_source[len('user: '):], _facts)


def window_input(prompt):
    if WINDOW_PREFIX in prompt:
        return json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
    return None


def replay_window(prompt, system, invoke):
    """Keep legacy consumer fixtures per source; native window tests bypass this adapter."""
    data = window_input(prompt)
    result = {'facts': [], }
    for span in data['pending']:
        if data['source_kind'] == 'dialogue' and span['role'] != 'user':
            continue
        raw = invoke('对话：\n' + span['content'] + '\n\nJSON 事实：', system=system)
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if isinstance(value, list):
            value = {'facts': value, }
        if not isinstance(value, dict):
            return raw
        for key in result:
            for item in value.get(key, []):
                if 'rules' in data:
                    fields = ('subject', 'predicate', 'object', 'text')
                    item = {k: item[k] for k in (*fields, 'categories', 'sensitive', 'time_kind', 'effective_date', 'retention') if k in item}
                    if key == 'facts':
                        categories = ['professional_details'] if item['predicate'] == 'works_at' else []
                        single = item['predicate'] in {'works_at', 'lives_in', 'current_city', 'occupation',
                                                        'works_remotely', 'favorite_food', 'favorite_color',
                                                        'favorite_drink', 'studying', 'job_title', 'does_yoga'}
                        durable = single or item['predicate'] in {'likes', 'dislikes', 'loves', 'prefers'}
                        item = dict(categories=categories, sensitive=False, time_kind='current', effective_date=None,
                                    retention='durable' if durable else 'normal') | item
                        item.setdefault('text', f"{item['subject']} {item['predicate']} {item['object']}")
                result[key].append({**item, 'source_ids': [span['id']]})
    if 'rules' in data:
        result['digests'] = [dict(source_id=span['id'], text='Synthetic event digest')
                               for span in data['pending'] if span['id'] in data['rules']['digest_required']]
    if data['operation'] == 'digest' or '本次仅补缺失摘要' in system:
        result = {'digests': result['digests']}
    if 'entity_types' in data:
        from tests.postgres.ontology_fixtures import typed_result
        result = typed_result(result)
    return json.dumps(result, ensure_ascii=False)


def comparison_items(prompt):
    data = json.loads(prompt)
    return [dict(item, first=data['facts'][item['first']], second=data['facts'][item['second']])
            for item in data['items']]


def adjudication_reply(prompt):
    """Fixed decisions for the legacy consumer fixtures; native tests specify verdicts."""
    decisions = []
    for pair in comparison_items(prompt):
        first, second = pair['first'], pair['second']
        replaces = first['predicate'] == second['predicate'] and second['predicate'] in {'works_at', 'lives_in', 'current_city', 'occupation', 'works_remotely', 'favorite_food', 'favorite_color', 'favorite_drink', 'studying', 'job_title', 'does_yoga'}
        reverses = ({first['predicate'], second['predicate']} in ({'likes', 'dislikes'}, {'loves', 'dislikes'})
                    and first['object'] == second['object'])
        decisions.append(dict(id=pair['id'], decision='conflict' if replaces or reverses else 'coexists',
                              reason='Synthetic comparison'))
    return json.dumps(dict(decisions=decisions))


class RecordedLLM:
    def complete(self, prompt, system='', **kwargs):
        if system.startswith('MNEMOTRACE_RELATION_RESOLUTION'):
            from tests.postgres.ontology_fixtures import ontology_reply
            return ontology_reply(prompt, system)
        if system.startswith('MNEMOTRACE_FACT_COMPARISON'):
            return adjudication_reply(prompt)
        if window_input(prompt) is not None:
            return replay_window(prompt, system, self.complete)
        if '对话：\n' in prompt and '\n\nJSON 事实：' in prompt:
            content = prompt.split('对话：\n', 1)[1].rsplit('\n\nJSON 事实：', 1)[0]
            if content not in RESPONSES:
                raise AssertionError('Missing synthetic model response: ' + repr(content))
            return json.dumps({'facts': RESPONSES[content], }, ensure_ascii=False)
        if '<session>' in prompt:
            return '[]'
        if '用户事实：' in prompt:
            raise RuntimeError('No synthetic persona response configured')
        return ''


class FixtureProtocol:
    """Give existing list-based test data the explicit new envelope, only in tests."""
    def __init__(self, model):
        self.model = model

    def complete(self, prompt, **kwargs):
        if (kwargs.get('system') or '').startswith('MNEMOTRACE_FACT_COMPARISON'):
            return adjudication_reply(prompt)
        if window_input(prompt) is not None:
            return replay_window(prompt, kwargs.get('system', ''), self.complete)
        raw = self.model.complete(prompt, **kwargs)
        if '对话：\n' in prompt and '\n\nJSON 事实：' in prompt:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                return raw
            if isinstance(data, list):
                return json.dumps({'facts': data, }, ensure_ascii=False)
        return raw


class MemoryEngine(NativeMemory):
    def __init__(self, *args, **kwargs):
        if len(args) < 2:
            kwargs.setdefault('embedder', MockEmbedder())
        if len(args) < 3:
            if 'llm' not in kwargs:
                kwargs['llm'] = RecordedLLM()
            elif kwargs['llm'] is not None:
                kwargs['llm'] = FixtureProtocol(kwargs['llm'])
        super().__init__(*args, **kwargs)


def seed_digests(memory, episodes):
    """Seed pre-existing digests for read/restore tests, without simulating an LLM."""
    for supplied in episodes:
        ep = memory.episodes_doc.get(supplied.id)
        ep.digest = ep.content
        ep.digest_embedding = memory.embedder.embed(ep.digest)
        memory._sync_episode(ep)


def _fixture_entity_id(user_id, name):
    from hashlib import sha256

    return 'synthetic_' + sha256(f'{user_id}\0{name.lower()}'.encode()).hexdigest()


def seed_linked(memory, subject, predicate, object_, *, user_id='default', **fields):
    """Assign explicit entity IDs to the nodes of a synthetic test graph."""

    return memory.add_fact(subject, predicate, object_, user_id=user_id,
                           subject_id=_fixture_entity_id(user_id, subject),
                           object_id=_fixture_entity_id(user_id, object_), **fields)


def link_synthetic_facts(memory, predicates):
    """Attach fixed endpoints before testing graph consumers of recorded model outputs."""
    for fact in memory._all_facts():
        if fact.predicate in predicates:
            fact.subject_id = _fixture_entity_id(fact.user_id, fact.subject)
            fact.object_id = _fixture_entity_id(fact.user_id, fact.object)
            memory._upsert_fact(fact)
            memory.writer.graph_builder.add_fact(fact)


def seed_timeline(memory, subject, predicate, object_, **fields):
    """Apply fixed contradictions against previous values in this synthetic slot."""
    from mnemotrace.consolidate.adjudication import plan_merge
    from mnemotrace.util import now

    prior = {fact.id: fact for fact in memory._all_facts()}
    incoming = memory.add_fact(subject, predicate, object_, **fields)
    verdicts = {tuple(sorted((fact.id, incoming.id))): 'conflict'
                for fact in prior.values() if fact.id != incoming.id and fact.slot == incoming.slot}
    changed, conflicts, _ = plan_merge([incoming], prior.values(), None, memory.config, None, now(),
                                       verdicts=verdicts)
    assert not conflicts
    for fact in changed:
        original = incoming if fact.id == incoming.id else prior[fact.id]
        original.invalid_at, original.expired_at, original.supersedes = fact.invalid_at, fact.expired_at, fact.supersedes
        memory._upsert_fact(original)
        memory.writer.graph_builder.add_fact(original)
    return incoming


def saved_outcome(mem, *, user_id, session_id, kind, statement, why='', provenance=()):
    """Seed the stored representation of a historical conclusion."""
    from mnemotrace.types import Fact

    fact = Fact(subject=session_id, predicate=kind, object=statement,
                text=statement + (f' （依据：{why}）' if why else ''), user_id=user_id,
                provenance=list(provenance))
    fact.sensitive = bool(why)
    fact.embedding = mem.embedder.embed(fact.text)
    mem._upsert_fact(fact)
    mem.writer.graph_builder.add_fact(fact)
    return fact
