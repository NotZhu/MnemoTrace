"""Cross-stage invalidation, standalone concurrency, explicit retry and crash checkpoints."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from mnemotrace import MemoryEngine
from mnemotrace._pipeline import run_one
from mnemotrace.consolidate.pipeline import PipelineTasks, validate_state
from mnemotrace.llm import FakeLLM
from tests.test_message_pipeline import message, reply
from tests.test_fact_adjudication import decisions


def test_native_concurrent_receive_is_ordered_and_isolated():
    mem = MemoryEngine()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: mem.remember_messages([message(str(n))], 's', 'u'), range(12)))
    assert sorted(r['messages'][0]['sequence'] for r in results) == list(range(1, 13))
    assert mem.remember_messages([message('0')], 's', 'other')['messages'][0]['sequence'] == 1
    validate_state(mem._pipeline_state, mem.episodes_doc.values())


def test_native_model_allows_append_and_close_prevents_late_commit():
    entered, release = threading.Event(), threading.Event()
    def respond(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    mem.remember_messages([message('a')], 's', 'u')
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(mem.flush_messages, 'u', 's', .2)
        assert entered.wait(5)
        appended = pool.submit(mem.remember_messages, [message('b')], 's', 'u')
        assert appended.result(5)['messages'][0]['sequence'] == 2
        mem.close()
        release.set()
        assert not work.result(5)['ok']
    assert not mem.fact_store.values()


def test_source_time_comes_from_matching_role():
    def respond(prompt, system):
        data = json.loads(prompt.split('MNEMOTRACE_WINDOW\n', 1)[1])
        return json.dumps(dict(facts=[dict(category='其他', sensitive=False, time_kind='current', effective_date=None, cardinality='unknown', retention='unknown', subject='user', predicate='lives_in', object='Synthetic city',
            text='Synthetic city', source_ids=[s['id'] for s in data['pending']])],
            digests=[]))
    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    mem.remember_messages([message('u', event_time=None), message('a', role='assistant', event_time=200)], 's', 'u')
    assert mem.flush_messages('u')['ok']
    assert mem.fact_store.values()[0].valid_at is None


def test_reembedding_does_not_authorize_generation_from_an_obsolete_policy():
    from mnemotrace._pipeline import memory_adapter
    from tests.embedder_fixtures import MockEmbedder
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    host = memory_adapter(mem, 'u')
    PipelineTasks(mem, 'u').force()
    assert run_one(host, 'u')
    window, = PipelineTasks(mem, 'u').state['windows'].values()
    assert window['phase'] == 'embed'
    mem.policy['extract_instruction'] = 'Synthetic changed extraction policy'
    mem.reembed(MockEmbedder(32))
    assert run_one(host, 'u')
    assert not mem.fact_store.values() and len(mem.llm.calls) == 1
    assert window['state'] == 'failed' and window['error_code'] == 'configuration_changed'


def test_core_and_digest_evidence_are_strict_on_restore():
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    assert mem.flush_messages('u')['ok']
    ep = mem.episodes_doc.values()[0]
    del ep.coverage['unit']
    with pytest.raises(ValueError):
        validate_state(mem._pipeline_state, mem.episodes_doc.values())


def test_manual_fact_keeps_authority_over_dialogue_fact():
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    old = mem.add_fact('user', 'lives_in', 'A', 'u')
    old.source, old.valid_at = 'extracted', 100
    mem.fact_store.upsert(old.id, old.embedding, old)
    new = mem.add_fact('user', 'lives_in', 'B', 'u', valid_at=200, cardinality='single')
    assert not mem.fact_store.get(old.id).is_live() and new.source == 'user'


def test_long_history_adapter_keeps_global_positional_ids():
    mem = MemoryEngine()
    rows = [dict(role='user', content='Synthetic repeated content') for _ in range(1001)]
    result = mem.import_messages([dict(session_id='s', messages=rows)], consolidate=False)
    assert result['episodes'] == 1001
    assert len({e.message_id for e in mem.episodes_doc.values()}) == 1001
    validate_state(deepcopy(mem._pipeline_state), mem.episodes_doc.values())


def test_missing_identity_record_cannot_restore_as_a_new_message():
    mem = MemoryEngine()
    accepted = mem.remember_messages([message('a')], 's', 'u')
    state = deepcopy(mem._pipeline_state)
    del state['namespaces']['u']['sources'][accepted['messages'][0]['episode_id']]
    with pytest.raises(ValueError):
        validate_state(state, mem.episodes_doc.values())


def test_native_missing_model_fails_without_waiting_for_retry():
    mem = MemoryEngine()
    mem.remember_messages([message('a')], 's', 'u')
    result = mem.flush_messages('u', timeout=.1)
    assert result['status'] == 'failed'
    assert result['jobs'][0]['attempts'] == 1
    assert result['jobs'][0]['error_code'] == 'llm_not_configured'


def test_late_unknown_and_older_history_cannot_replace_newer_source():
    def respond(prompt, system):
        if 'pair id' in system:
            return decisions(prompt)
        result = json.loads(reply(prompt, system))
        for fact in result['facts']:
            fact['predicate'] = 'lives_in'
        return json.dumps(result)
    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    for mid, stamp, mode in [('new', 200, 'live'), ('unknown', None, 'historical'), ('old', 100, 'historical')]:
        mem.remember_messages([message(mid, mid, event_time=stamp)], 's', 'u', mode=mode)
        assert mem.flush_messages('u')['ok']
    latest = [f.object for f in mem.fact_store.values() if f.is_live()]
    assert set(latest) == {'new', 'unknown'}


@pytest.mark.parametrize('arrivals,expected', [
    ([('A', 100), ('C', 300), ('B', 200), ('Z', 50)], [('Z', 50, 100), ('A', 100, 200), ('B', 200, 300), ('C', 300, None)]),
    ([('A', 300), ('B', 200), ('A', 100)], [('A', 100, 200), ('B', 200, 300), ('A', 300, None)]),
])
def test_out_of_order_history_has_disjoint_intervals_and_a_complete_chain(arrivals, expected):
    def respond(prompt, system):
        if 'pair id' in system:
            return decisions(prompt)
        result = json.loads(reply(prompt, system))
        for fact in result['facts']:
            fact['predicate'] = 'lives_in'
        return json.dumps(result)
    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    for index, (value, stamp) in enumerate(arrivals):
        mem.remember_messages([message(str(index), value, event_time=stamp)], 's', 'u', mode='historical')
        assert mem.flush_messages('u')['ok']
    facts = sorted(mem.fact_store.values(), key=lambda f: f.valid_at)
    assert [(f.object, f.valid_at, f.invalid_at) for f in facts] == expected
    assert [f.supersedes for f in facts] == [None, *[f.id for f in facts[:-1]]]
    assert [f.object for f in facts if f.is_live(250)] == ['B']
    relations = {r.fact_id: r for r in mem.graph.relations()}
    assert all((relations[f.id].valid_at, relations[f.id].invalid_at) == (f.valid_at, f.invalid_at) for f in facts)


def test_equal_time_facts_do_not_retire_each_other_in_model_order():
    def respond(prompt, system):
        if 'pair id' in system:
            return decisions(prompt)
        result = json.loads(reply(prompt, system))
        for fact in result['facts']:
            fact['predicate'] = 'lives_in'
        result['facts'].reverse()
        return json.dumps(result)
    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    mem.remember_messages([message('a', 'old', event_time=100), message('b', 'new', event_time=100)], 's', 'u')
    assert mem.flush_messages('u')['ok']
    assert {f.object for f in mem.fact_store.values() if f.is_live()} == {'new', 'old'}
    assert len(mem.pending_conflicts('u')) == 1


def test_cached_model_configuration_identity_tracks_the_inner_provider(tmp_path):
    from mnemotrace.llm.cache import CachedLLM
    from mnemotrace.consolidate.pipeline import config_identity
    model = FakeLLM(handler=reply)
    model.model = 'synthetic-model-A'
    mem = MemoryEngine(llm=CachedLLM(model, str(tmp_path / 'cache'), model_tag='synthetic-tag'))
    before = config_identity(mem)
    model.model = 'synthetic-model-B'
    assert config_identity(mem) != before


def test_conflict_cancelled_requires_every_job_to_be_conflict():
    """#16：_conflict_cancelled 的混合失败分支必须有测试。

    过去把内部的 all 改成 any，回归仍全绿；这里直接锁定“只要有一个非冲突失败
    （如 embedding_failed）就不是良性冲突”的行为。"""
    from mnemotrace._pipeline import _conflict_cancelled
    only_conflict = dict(jobs=[dict(state='failed', error_code='manual_change'),
                               dict(state='cancelled', error_code='source_changed')])
    assert _conflict_cancelled(only_conflict) is True
    mixed = dict(jobs=[dict(state='failed', error_code='manual_change'),
                       dict(state='failed', error_code='embedding_failed')])
    assert _conflict_cancelled(mixed) is False
    single_non_conflict = dict(jobs=[dict(state='failed', error_code='extraction_failed')])
    assert _conflict_cancelled(single_non_conflict) is False
    assert _conflict_cancelled(dict(jobs=[])) is False


def test_extraction_conflict_importable_and_carries_ids():
    """#19：409 extraction_conflict 的对外契约要有守护——异常可从包根导入、列入 __all__、
    且带 stored_raw/episode_ids。仅断言可导入不足：删掉 __all__ 条目时模块属性仍在，
    契约已经偏离但测试仍绿。"""
    import mnemotrace
    from mnemotrace import ExtractionConflictError
    assert mnemotrace.ExtractionConflictError is ExtractionConflictError
    assert 'ExtractionConflictError' in mnemotrace.__all__
    exc = ExtractionConflictError(stored_raw=True, episode_ids=['e1', 'e2'])
    assert exc.code == 'extraction_conflict' and exc.status_code == 409
    assert exc.detail() == {'code': 'extraction_conflict', 'message': exc.message,
                            'stored_raw': True, 'episode_ids': ['e1', 'e2']}

def test_raise_for_failed_result_tolerates_core_success_aux_failure():
    """核心事实已落库、仅摘要等辅助步骤失败时，不应抛错（与 execute_sources 一致），
    否则 immediate 客户端会误以为整批失败而重试，可能重复写入。"""
    from mnemotrace._pipeline import raise_for_failed_result, extract_partial_success
    # core 完成、摘要失败
    partial = dict(ok=False, status='failed', jobs=[
        dict(episode_id='a', core='completed', summarization='failed', state='failed',
             error_code='digest_invalid_response')])
    assert extract_partial_success(partial, {'a'}) is True
    raise_for_failed_result(partial, {'a'})  # 不抛
    # 核心本身失败 -> 必须抛
    core_failed = dict(ok=False, status='failed', jobs=[
        dict(episode_id='a', core='failed', summarization='pending', state='failed',
             error_code='extraction_failed')])
    assert extract_partial_success(core_failed, {'a'}) is False
    with pytest.raises(Exception):
        raise_for_failed_result(core_failed, {'a'})
    # 辅助失败但核心未全部完成（缺 id）-> 抛
    incomplete = dict(ok=False, status='failed', jobs=[
        dict(episode_id='a', core='completed', summarization='failed', state='failed',
             error_code='digest_invalid_response')])
    assert extract_partial_success(incomplete, {'a', 'b'}) is False
    with pytest.raises(Exception):
        raise_for_failed_result(incomplete, {'a', 'b'})
