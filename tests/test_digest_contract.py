"""Native boundaries reject retired fields and preserve source-bound digests."""
import json

import pytest

from mnemotrace import MemoryEngine
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.extraction import ExtractionResponseError
from mnemotrace.llm import FakeLLM
from mnemotrace.store._validation import validate_snapshot


@pytest.mark.parametrize('retired', ['working', 'summaries'])
def test_complete_old_response_is_rejected_without_committing(retired):
    response = dict(facts=[], digests=[])
    response[retired] = []
    memory = MemoryEngine(llm=FakeLLM(responses=[json.dumps(response)]))
    source = memory.add('Synthetic source to retain')
    with pytest.raises(ExtractionResponseError):
        memory.consolidate()
    assert memory.get_episode(source.id)['content'] == source.content
    assert not source.consolidated and not memory._all_facts()


def test_agent_task_remains_source_evidence_without_a_task_record():
    task = '帮我整理发布清单'
    memory = MemoryEngine(llm=FakeLLM(responses=['{"facts":[],"digests":[]}']))
    memory.remember_messages([dict(message_id='task', role='user', content=task)], 'release')
    assert memory.flush_messages(session_id='release')['ok']
    assert not memory._all_facts()
    assert memory.retrieve_digests(task) == []
    assert next(iter(memory.episodes_doc.values())).content == task
    assert 'WORKING MEMORY' not in memory.lean_context(task)
    assert not hasattr(memory, 'working_mem')


def test_digest_is_selected_while_explicit_source_keeps_original():
    original = 'Synthetic source with exact value 37. ' * 100
    def respond(prompt, system):
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        source, = data['pending']
        result = dict(digests=[dict(source_id=source['id'], text='Exact value 37.')])
        if data['operation'] == 'extract':
            result['facts'] = []
        return json.dumps(result)
    memory = MemoryEngine(llm=FakeLLM(handler=respond))
    source = memory.add(original)
    assert memory.generate_digests([source]) == 1
    evidence, = memory.retrieve_digests('value 37')
    assert evidence.text == 'Exact value 37.' and evidence.representation == 'digest'
    assert memory.get_episode(source.id)['content'] == original


@pytest.mark.parametrize('field', ['summary', 'summary_coverage'])
def test_snapshot_does_not_silently_drop_old_source_fields(field):
    snapshot = dict(format_version=3, facts=[], episodes=[dict(id='source', content='Synthetic source',
                    session_id='s', role='user', event_time=1, **{field: 'obsolete'})])
    with pytest.raises(ValueError, match=field):
        validate_snapshot(snapshot)


def test_remember_and_recall_reject_retired_parameters():
    memory = MemoryEngine()
    with pytest.raises(TypeError, match='scope'):
        memory.remember('Synthetic task', scope='raw')
    with pytest.raises(TypeError, match='mode'):
        memory.remember_messages([dict(message_id='m', role='user', content='Synthetic dialogue')], 's', mode='live')
    from mnemotrace.types import Episode
    for kind in ('raw', 'document'):
        with pytest.raises(ValueError, match='source_kind'):
            Episode('Synthetic source', source_kind=kind)
    assert not hasattr(memory, 'import_messages') and not hasattr(memory, 'import_data')
    with pytest.raises(ValueError, match='extract must be a boolean'):
        memory.remember('Synthetic task', extract='false')
    assert not memory.episodes_doc.values()
    assert memory.add('Synthetic note').source_kind == 'note'
    with pytest.raises(TypeError, match='session_id'):
        memory.lean_context('Synthetic query', session_id='s')
    with pytest.raises(TypeError, match='n_summaries'):
        memory.lean_context('Synthetic query', n_summaries=1)


def test_snapshot_rejects_retired_working_collection():
    with pytest.raises(ValueError, match='snapshot.working'):
        validate_snapshot(dict(format_version=3, facts=[], episodes=[], working=[]))
