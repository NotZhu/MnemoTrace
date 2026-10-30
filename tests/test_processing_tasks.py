import json

import pytest

from mnemotrace import Memory
from mnemotrace.consolidate.tasks import ProcessingTasks
from mnemotrace.store import StoreFormatError


def test_tasks_survive_snapshot_and_no_historical_adoption(tmp_path):
    mem = Memory()
    mem.add('historical', user_id='u')
    ep = mem.add('new synthetic', user_id='u', session_id='s')
    tasks = ProcessingTasks(mem, 'u')
    tasks.enqueue(ep, 100)
    tasks.mark([ep.id], 'running', 101, attempts=1)
    mem.save(str(tmp_path))
    restored = Memory.open(str(tmp_path))
    recovered = ProcessingTasks(restored, 'u')
    recovered.reconcile(102)
    assert list(recovered.rows()) == [ep.id]
    assert recovered.rows()[ep.id]['state'] == 'pending'
    assert recovered.rows()[ep.id]['attempts'] == 1


@pytest.mark.parametrize('mutation', ['missing', 'owner', 'attempts', 'source'])
def test_corrupt_task_state_is_rejected_before_restore(tmp_path, mutation):
    mem = Memory()
    ep = mem.add('new synthetic', user_id='u')
    ProcessingTasks(mem, 'u').enqueue(ep, 100)
    mem.save(str(tmp_path))
    path = tmp_path / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    tasks = data['state']['processing']
    if mutation == 'missing':
        tasks.pop('generation')
    elif mutation == 'owner':
        tasks['namespace'] = 'other'
    elif mutation == 'source':
        tasks['jobs']['missing_source'] = tasks['jobs'].pop(ep.id)
    else:
        tasks['jobs'][ep.id]['attempts'] = True
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(StoreFormatError):
        Memory.open(str(tmp_path))


def test_cancel_and_retry_are_explicit_and_keep_completed_jobs():
    mem = Memory()
    ep = mem.add('synthetic', user_id='u')
    tasks = ProcessingTasks(mem, 'u')
    tasks.enqueue(ep, 100)
    tasks.cancel(101)
    tasks.force([ep.id], 102)
    assert tasks.rows()[ep.id]['state'] == 'cancelled'
    tasks.force([ep.id], 103, retry_failed=True)
    assert tasks.rows()[ep.id]['state'] == 'pending'
    assert tasks.rows()[ep.id]['forced']


def test_task_manifest_rejects_uncommitted_episode_progress(tmp_path):
    from mnemotrace.llm import FakeLLM
    mem = Memory(llm=FakeLLM(responses=[json.dumps({'facts': [
        {'subject': 'user', 'predicate': 'owns', 'object': 'synthetic'}], 'working': []})]))
    ep = mem.add('synthetic', user_id='u')
    tasks = ProcessingTasks(mem, 'u')
    tasks.enqueue(ep, 100)
    mem.save(str(tmp_path))
    manifest = (tmp_path / 'manifest.json').read_bytes()
    mem.consolidate([ep])
    tasks.completed([ep.id], 101)
    mem.save()
    # Simulate collections replaced but the new manifest not committed.
    (tmp_path / 'manifest.json').write_bytes(manifest)
    with pytest.raises(StoreFormatError):
        Memory.open(str(tmp_path))
