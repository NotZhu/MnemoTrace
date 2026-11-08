"""Recorded synthetic model outputs for existing pipeline regressions.

These fixtures test consumers of extracted facts, not language understanding.
There is no sentence grammar or semantic inference in this test double.
"""
from __future__ import annotations

import json
from pathlib import Path

from mnemotrace import MemoryEngine as NativeMemory
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
    result = {'facts': [], 'working': []}
    for span in data['pending']:
        if data['source_kind'] == 'dialogue' and span['role'] != 'user':
            continue
        raw = invoke('对话：\n' + span['content'] + '\n\nJSON 事实：', system=system)
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if isinstance(value, list):
            value = {'facts': value, 'working': []}
        if not isinstance(value, dict):
            return raw
        for key in result:
            for item in value.get(key, []):
                if 'rules' in data:
                    if key == 'working' and data['mode'] == 'historical':
                        continue
                    fields = ('subject', 'predicate', 'object', 'text') if key == 'facts' else ('content', 'kind')
                    item = {k: item[k] for k in (*fields, 'topic', 'category', 'sensitive', 'time_kind', 'effective_date') if k in item}
                    if key == 'facts':
                        category = '工作' if item['predicate'] == 'works_at' else '其他'
                        item = dict(category=category, sensitive=False, time_kind='current', effective_date=None) | item
                        item.setdefault('text', f"{item['subject']} {item['predicate']} {item['object']}")
                result[key].append({**item, 'source_ids': [span['id']]})
    if 'rules' in data:
        result['summaries'] = [dict(source_id=span['id'], text='Synthetic event summary')
                               for span in data['pending'] if span['id'] in data['rules']['summary_required']]
    if data['operation'] == 'summary' or '本次仅补缺失摘要' in system:
        result = {'summaries': result['summaries']}
    return json.dumps(result, ensure_ascii=False)


def adjudication_reply(prompt):
    """Fixed decisions for the legacy consumer fixtures; native tests specify verdicts."""
    from mnemotrace.consolidate.conflict import is_single_valued

    decisions = []
    for pair in json.loads(prompt):
        first, second = pair['first'], pair['second']
        replaces = first['predicate'] == second['predicate'] and is_single_valued(first['predicate'])
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
            return json.dumps({'facts': RESPONSES[content], 'working': []}, ensure_ascii=False)
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
                return json.dumps({'facts': data, 'working': []}, ensure_ascii=False)
        return raw


class MemoryEngine(NativeMemory):
    def __init__(self, *args, **kwargs):
        if len(args) < 3:
            if 'llm' not in kwargs:
                kwargs['llm'] = RecordedLLM()
            elif kwargs['llm'] is not None:
                kwargs['llm'] = FixtureProtocol(kwargs['llm'])
        super().__init__(*args, **kwargs)


def seed_summaries(memory, episodes):
    """Seed pre-existing summaries for read/restore tests, without simulating an LLM."""
    for supplied in episodes:
        ep = memory.episodes_doc.get(supplied.id)
        ep.summary = ep.content
        ep.summary_embedding = memory.embedder.embed(ep.summary)
        memory._sync_episode(ep)


def saved_outcome(mem, *, user_id, session_id, kind, statement, why='', provenance=()):
    """Seed the stored representation of a historical conclusion."""
    from mnemotrace.consolidate.outcomes import OUTCOME_CATEGORY
    from mnemotrace.types import Fact

    fact = Fact(subject=session_id, predicate=kind, object=statement, display=statement,
                text=statement + (f' （依据：{why}）' if why else ''), user_id=user_id,
                provenance=list(provenance))
    fact.category = OUTCOME_CATEGORY
    fact.sensitive = bool(why)
    fact.embedding = mem.embedder.embed(fact.text)
    mem._upsert_fact(fact)
    mem.writer.graph_builder.add_fact(fact)
    return fact
