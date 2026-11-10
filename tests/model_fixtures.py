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
                    item = {k: item[k] for k in (*fields, 'categories', 'object_kind', 'sensitive', 'time_kind', 'effective_date', 'cardinality', 'retention') if k in item}
                    if key == 'facts':
                        categories = ['professional_details'] if item['predicate'] == 'works_at' else []
                        single = item['predicate'] in {'works_at', 'lives_in', 'current_city', 'occupation',
                                                        'works_remotely', 'favorite_food', 'favorite_color',
                                                        'favorite_drink', 'studying', 'job_title', 'does_yoga'}
                        durable = single or item['predicate'] in {'likes', 'dislikes', 'loves', 'prefers'}
                        item = dict(categories=categories, object_kind='unknown', sensitive=False, time_kind='current', effective_date=None, cardinality='single' if single else 'multi',
                                    retention='durable' if durable else 'normal') | item
                        item.setdefault('text', f"{item['subject']} {item['predicate']} {item['object']}")
                result[key].append({**item, 'source_ids': [span['id']]})
    if 'rules' in data:
        result['digests'] = [dict(source_id=span['id'], text='Synthetic event digest')
                               for span in data['pending'] if span['id'] in data['rules']['digest_required']]
    if data['operation'] == 'digest' or '本次仅补缺失摘要' in system:
        result = {'digests': result['digests']}
    return json.dumps(result, ensure_ascii=False)


def adjudication_reply(prompt):
    """Fixed decisions for the legacy consumer fixtures; native tests specify verdicts."""
    decisions = []
    for pair in json.loads(prompt):
        first, second = pair['first'], pair['second']
        replaces = first['predicate'] == second['predicate'] and second['cardinality'] == 'single'
        reverses = ({first['predicate'], second['predicate']} in ({'likes', 'dislikes'}, {'loves', 'dislikes'})
                    and first['object'] == second['object'])
        decisions.append(dict(id=pair['id'], decision='replaces' if replaces or reverses else 'coexists'))
    return json.dumps(dict(decisions=decisions))


class RecordedLLM:
    def complete(self, prompt, system='', **kwargs):
        if prompt.startswith('[') and system and 'pair id' in system:
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
        if prompt.startswith('[') and 'pair id' in (kwargs.get('system') or ''):
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


def saved_outcome(mem, *, user_id, session_id, kind, statement, why='', provenance=()):
    """Seed the stored representation of a historical conclusion."""
    from mnemotrace.types import Fact

    fact = Fact(subject=session_id, predicate=kind, object=statement, display=statement,
                text=statement + (f' （依据：{why}）' if why else ''), user_id=user_id,
                provenance=list(provenance))
    fact.sensitive = bool(why)
    fact.embedding = mem.embedder.embed(fact.text)
    mem._upsert_fact(fact)
    mem.writer.graph_builder.add_fact(fact)
    return fact
