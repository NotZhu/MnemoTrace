"""Incremental task framing and atomic rejection of the recorded mixed response."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from mnemotrace import Memory, ExtractionResponseError
from mnemotrace.consolidate.llm_extractor import EXTRACT_SYSTEM, LLMExtractor
from mnemotrace.consolidate.processing import PROGRESS_KEY
from mnemotrace.consolidate.windows import ExtractionWindow, SourceSpan, WINDOW_PREFIX
from mnemotrace.llm import FakeLLM
from mnemotrace.types import Episode

FIXTURE = json.loads(Path(__file__).with_name('extraction_context_response.json').read_text(encoding='utf-8'))


def recorded_window():
    def spans(group):
        return tuple(SourceSpan(Episode(row['content'], id=row['id'], user_id='u', session_id='s',
            event_time=index+1+(len(FIXTURE['context']) if group=='pending' else 0),
            ingested_at=index+1, metadata={'memory_scope':'auto'}),0,len(row['content']))
            for index,row in enumerate(FIXTURE[group]))
    return ExtractionWindow(spans('pending'),spans('context'))


def test_window_request_frames_incremental_work_and_keeps_new_input_last():
    window = recorded_window()
    model = FakeLLM(responses=['{"facts":[],"working":[]}'])
    extractor = LLMExtractor(model)
    size = extractor.window_size(window)
    extractor.raw_window(window, 2048)
    call = model.calls[-1]
    prompt, system = call['prompt'], call['system']
    assert system.startswith('你是记忆增量提取引擎。')
    assert '"facts":[],"working":[' in system  # The working-only example, not just empty output.
    assert 'source_ids' in system
    assert '不要只保留最后状态' in system
    assert '不重复已有三元组' in system
    assert '主体、指代或含义' in system
    assert '"source_ids":["C","P"]' in system
    assert prompt.endswith('本次新增记忆 JSON：')
    value = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX,1)[1])[0]
    assert list(value) == ['context','pending']
    assert [s['id'] for s in value['pending']] == [s.id for s in window.pending]
    assert len(system.encode()) <= FIXTURE['baseline_system_bytes']
    assert size == len((prompt+system).encode())
    legacy_prompt='对话：\n'+window.content+'\n\nJSON 事实：'
    assert size <= len(legacy_prompt.encode())+FIXTURE['baseline_system_bytes']


def test_raw_episode_default_prompt_is_unchanged():
    assert hashlib.sha256(EXTRACT_SYSTEM.encode()).hexdigest() == FIXTURE['raw_system_sha256']
    model = FakeLLM(responses=['{"facts":[],"working":[]}'])
    extractor = LLMExtractor(model)
    extractor.raw_items(Episode('合成原文'))
    assert model.calls[0]['system'].startswith(EXTRACT_SYSTEM)
    assert model.calls[0]['prompt'].endswith('JSON 事实：')


def test_long_route_does_not_demonstrate_working_output():
    from dataclasses import replace
    window=recorded_window()
    episode=replace(window.pending[0].episode,metadata={'memory_scope':'long'})
    window=ExtractionWindow((SourceSpan(episode,0,len(episode.content)),),window.context)
    model=FakeLLM(responses=['{"facts":[],"working":[]}'])
    extractor=LLMExtractor(model)
    extractor.raw_window(window,2048)
    system=model.calls[0]['system']
    assert system.endswith('仅抽取长期事实，working 必须为空。')
    assert '"source_ids":["P"]' not in system
    assert 'C/P 仅为示例' in system
    assert extractor.window_size(window)==len((system+model.calls[0]['prompt']).encode())


def test_custom_policy_and_template_are_preserved_and_measured():
    window = recorded_window()
    model = FakeLLM(responses=['{"facts":[],"working":[]}'])
    extractor = LLMExtractor(model, system='合成自定义抽取策略',template='{role}\n{content}\n自定义输出',llm_topics=False)
    extractor.instruction = '保留合成标识'
    size = extractor.window_size(window)
    extractor.raw_window(window,2048)
    call = model.calls[-1]
    assert call['system'].startswith('合成自定义抽取策略')
    assert '保留合成标识' in call['system'] and 'source_ids' in call['system']
    assert call['prompt'].endswith('自定义输出')
    assert size == len((call['prompt']+call['system']).encode())


@pytest.mark.parametrize(('source','reason'),[(None,'missing_source_ids'),('not-an-array','invalid_source_ids'),
                                             (['synthetic-secret-id'],'unknown_source_id')])
def test_source_diagnostics_use_fixed_categories_without_values(source,reason,caplog):
    value=dict(facts=[dict(subject='user',predicate='likes',object='合成私有内容',source_ids=source)],working=[])
    extractor=LLMExtractor(FakeLLM(responses=[json.dumps(value)]))
    with pytest.raises(ExtractionResponseError) as error:
        extractor.raw_window(recorded_window(),2048)
    assert reason in caplog.text
    assert 'synthetic-secret-id' not in caplog.text and '合成私有内容' not in caplog.text
    assert error.value.detail()['code']=='extraction_invalid_response'


@pytest.mark.parametrize('count',[1,4])
def test_recorded_context_facts_with_valid_working_reject_atomically_and_can_retry(count,caplog):
    mode=['empty']
    def reply(prompt,system):
        if mode[0]=='empty':
            return '{"facts":[],"working":[]}'
        data=json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX,1)[1])[0]
        mapped={f'c{i+1}':s['id'] for i,s in enumerate(data['context'])}
        mapped.update({f'p{i+1}':s['id'] for i,s in enumerate(data['pending'])})
        value=deepcopy(FIXTURE['response'])
        value['facts']=value['facts'][:count] if mode[0]=='mixed' else []
        for group in ('facts','working'):
            for row in value[group]:
                row['source_ids']=[mapped[key] for key in row['source_ids']]
        return json.dumps(value,ensure_ascii=False)
    memory=Memory(llm=FakeLLM(handler=reply))
    for index,row in enumerate(FIXTURE['context']):
        ep=memory.add(row['content'],user_id='u',session_id='s',event_time=index+1)
        memory.consolidate([ep])
    memory.add_fact('u','likes','绿茶',user_id='u')
    before=deepcopy([f.__dict__ for f in memory._all_facts()])
    routed=memory.remember(FIXTURE['pending'][0]['content'],user_id='u',session_id='s')
    current=memory.episodes_doc.get(routed['episode_id'])
    mode[0]='mixed'
    with pytest.raises(ExtractionResponseError) as error:
        memory.consolidate([current])
    assert error.value.code=='extraction_invalid_response'
    assert 'context_only_source_ids' in caplog.text
    assert current.id not in caplog.text and '绿茶' not in caplog.text
    assert not memory.working_mem and not current.consolidated and PROGRESS_KEY not in current.metadata
    assert before==[f.__dict__ for f in memory._all_facts()]
    mode[0]='valid'
    memory.consolidate([current])
    assert current.consolidated and len(memory.working_mem)==1
    assert before==[f.__dict__ for f in memory._all_facts()]
    working,=memory.working_mem.values()
    assert working.metadata['provenance']==[current.id]
    memory.close()
