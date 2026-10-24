# 未完成事项的合成复现

仅保存当前未完成事项所需的原有复现及测量限制，不接触个人语料；默认不作为测试套件运行。问题状态见[台账](../../../docs/issues.md)。

<a id="evidence-issue-003"></a>

## ISSUE-003：方案决定进入偏好

给定提取结果会进入正式偏好；该复现不测量真实模型误提取的频率。

```powershell
@'
from mnemotrace.types import Episode
from mnemotrace.consolidate.llm_extractor import LLMExtractor
from mnemotrace.consolidate.structured import build_structured_profile

episode = Episode(content='For this deployment, I decided to use keyword retrieval.',
                  user_id='synthetic-user')
facts = LLMExtractor(None).facts_from(episode, [{
    'subject': 'user', 'predicate': 'prefers', 'object': 'keyword retrieval',
    'text': 'For this deployment, the user decided to use keyword retrieval.',
}])
profile = build_structured_profile(facts, subject='user', user_id='synthetic-user')
print(facts[0].predicate, profile['counts']['preferences'])
assert facts[0].predicate == 'prefers' and profile['counts']['preferences'] == 1
'@ | python -
```

默认 auto 路由单独验证，结果只说明该句命中瞬态标记，不证明显式 long 或导入路径也会被拦截。

```powershell
@'
import tempfile
from unittest.mock import patch
from mnemotrace.service import MemoryService

with patch('mnemotrace.llm.providers.load_dotenv'), patch.dict('os.environ', {
    'MNEMOTRACE_ANSWERER': '', 'MNEMOTRACE_VISION_LLM': '',
}):
    with tempfile.TemporaryDirectory(prefix='synthetic-routing-') as directory:
        service = MemoryService(data_dir=directory, embedder_name='hashing', llm_name='')
        result = service.remember('synthetic-user', '本次部署决定使用关键词检索。')
        facts = service.memories('synthetic-user')['facts']
        print({'scope': result['scope'], 'facts': len(facts)})
        assert result['scope'] == 'working' and not facts
'@ | python -
```

[返回事项](../../../docs/issues.md#issue-003)。

<a id="evidence-issue-004"></a>

## ISSUE-004：问答证据范围样本

样本中的“组装上下文 / 原始对话”估算分别为 646 / 139 与 747 / 139 tokens，涉及其他人物、
主题和跨层重复。两次完整上下文未逐段留存，无法将差值全部归因于提示词语言或重复信息。
两项估算覆盖的内容不同，也不等于模型计费量；后续应比较实际证据与答案质量。

[返回事项](../../../docs/issues.md#issue-004)。

<a id="evidence-issue-005"></a>

## ISSUE-005：会话结论同义重复

第二次没有新增原文，仅改写结论仍新增一条；第三次文字完全相同则被去重。

```powershell
@'
import json, tempfile
from unittest.mock import patch
from mnemotrace.service import MemoryService

statements = [
    'For the next six months, photography practice is scheduled every Saturday morning starting next month.',
    'Starting next month, Saturday mornings are reserved for photography practice for a six-month period.',
]
class SequenceLLM:
    def __init__(self):
        self.calls = 0
    def complete(self, *args, **kwargs):
        statement = statements[min(self.calls, 1)]
        self.calls += 1
        return json.dumps([{'kind': 'decision', 'statement': statement,
                            'why': 'An explicit settled schedule.'}])

with patch('mnemotrace.llm.providers.load_dotenv'), patch.dict('os.environ', {
    'MNEMOTRACE_ANSWERER': '', 'MNEMOTRACE_VISION_LLM': '',
    'MNEMOTRACE_SESSION_OUTCOMES': '1',
}):
    with tempfile.TemporaryDirectory(prefix='repeat-summary-') as directory:
        svc = MemoryService(data_dir=directory, embedder_name='hashing', llm_name='')
        svc.remember('synthetic-user', statements[0], session_id='synthetic-repeat')
        svc.llm = SequenceLLM()
        counts = [svc.close_session('synthetic-user', 'synthetic-repeat',
                  summarize=False, clear_working=False)['outcomes'] for _ in range(3)]
        rows = svc.memories('synthetic-user', kind='outcomes')['facts']
        print(json.dumps({'new_outcomes_each_round': counts,
                          'stored_outcomes': len(rows), 'llm_calls': svc.llm.calls}))
        assert counts == [1, 1, 0] and len(rows) == 2
'@ | python -
```

```json
{"new_outcomes_each_round": [1, 1, 0], "stored_outcomes": 2, "llm_calls": 3}
```

[返回事项](../../../docs/issues.md#issue-005)。

<a id="evidence-issue-006"></a>

## ISSUE-006：分类关键词误匹配

事实分类与画像分组分别复现；不能扩大为已确认的敏感信息过滤错误。

```powershell
@'
import sys
sys.stdout.reconfigure(encoding="utf-8")
from mnemotrace.consolidate.classify import classify
from mnemotrace.consolidate.structured import _category

print("事实 uses / storage engine:", classify("uses", "storage engine")[0])
for obj in (
    "team collaboration",
    "整理资料",
    "流行的编程框架",
    "保留事实和原始会话片段",
    "保留事实和原始会话记录",
):
    print("画像 prefers /", obj, "->", _category("prefers", obj))
'@ | python
```

```text
事实 uses / storage engine: 身份
画像 prefers / team collaboration -> 饮食
画像 prefers / 整理资料 -> 饮食
画像 prefers / 流行的编程框架 -> 音乐
画像 prefers / 保留事实和原始会话片段 -> 影视
画像 prefers / 保留事实和原始会话记录 -> 其他
```

[返回事项](../../../docs/issues.md#issue-006)。

<a id="evidence-issue-007"></a>

## ISSUE-007：画像候选入口不可达

置信度为 1.0 和 0.3 的给定偏好均进入正式画像；不证明真实模型已产生弱推断。

```powershell
@'
import json
from mnemotrace.types import Fact
from mnemotrace.consolidate.structured import build_structured_profile

for confidence in (1.0, 0.3):
    fact = Fact(subject='user', predicate='likes', object='photography',
                source='extracted', confidence=confidence,
                provenance=['synthetic-episode'], access_count=0)
    profile = build_structured_profile([fact], subject='user')
    print(json.dumps({'confidence': confidence, 'counts': profile['counts']}))
    assert profile['counts']['preferences'] == 1
    assert profile['counts']['tentative'] == 0
'@ | python -
```

```jsonl
{"confidence": 1.0, "counts": {"basic": 0, "preferences": 1, "tentative": 0, "habits": 0}}
{"confidence": 0.3, "counts": {"basic": 0, "preferences": 1, "tentative": 0, "habits": 0}}
```

[返回事项](../../../docs/issues.md#issue-007)。

<a id="evidence-issue-011"></a>

## ISSUE-011：单页上限与历史分页复现

仅使用内存合成事实，比较请求超过单页上限时的返回数量。

```powershell
@'
import tempfile
from unittest.mock import patch
from mnemotrace.service import MemoryService

with patch('mnemotrace.llm.providers.load_dotenv'), patch.dict('os.environ', {
    'MNEMOTRACE_ANSWERER': '', 'MNEMOTRACE_VISION_LLM': '',
}):
    with tempfile.TemporaryDirectory(prefix='pagination-limit-') as directory:
        service = MemoryService(data_dir=directory, embedder_name='hashing', llm_name='')
        # Hold one in-memory collection for the check; no snapshot is committed.
        with service.write_lock('synthetic-user'):
            memory = service.get('synthetic-user')
            for index in range(501):
                memory.add_fact('synthetic-user', f'attribute_{index}', f'value {index}',
                                user_id='synthetic-user', valid_at=index + 1)
            for limit in (500, 540, 600):
                page = service.memories('synthetic-user', facts_limit=limit, episodes_limit=0)
                print(limit, len(page['facts']), page['facts_page']['has_more'],
                      page['facts_page']['next_offset'])
'@ | python -B -
```

| 请求 facts_limit | 返回事实数 | has_more | next_offset |
| ---------------: | ---------: | -------- | ----------: |
|              500 |        500 | True     |         500 |
|              540 |        500 | True     |         500 |
|              600 |        500 | True     |         500 |

该用例检查服务端单页上限。旧页面曾只递增 limit，无法到达第 501 条；当前四个列表已改用 next_offset。这里不将历史页面缺陷描述为当前行为；剩余时间线需求见对应事项。

[返回事项](../../../docs/issues.md#issue-011)。

<a id="evidence-issue-014"></a>

## ISSUE-014：截断后过滤丢失当前关系

仅使用内存合成数据，比较“先截断再过滤”和“先过滤再截断”。

```powershell
@'
from mnemotrace import Memory

memory = Memory()
for index in range(121):
    fact = memory.add_fact('synthetic', 'tag', f'old-{index}', valid_at=1)
    fact.invalid_at = 2
    for relation in memory.graph.relations():
        if relation.fact_id == fact.id:
            relation.invalid_at = 2
memory.add_fact('synthetic', 'tag', 'CURRENT', valid_at=3)
page = memory.graph_data('default', live_only=False, limit=120)
current = memory.graph_data('default', live_only=True, limit=120)
visible = [edge for edge in page['edges'] if edge['live']]
print({'page_edges': len(page['edges']), 'local_current': len(visible),
       'server_current': len(current['edges'])})
assert len(page['edges']) == 120 and not visible and len(current['edges']) == 1
'@ | python -
```

[返回事项](../../../docs/issues.md#issue-014)。

<a id="evidence-issue-017"></a>

## ISSUE-017：习惯正文忽略展示字段

已有中文 display 时，习惯卡片仍取内部 text；复现不涉及事实丢失。

```powershell
@'
import sys
sys.stdout.reconfigure(encoding='utf-8')
from mnemotrace.types import Fact
from mnemotrace.localize import display_of
from mnemotrace.consolidate.structured import build_structured_profile

fact = Fact(subject='user', predicate='weekly_reads', object='示例书',
            display='用户每周阅读示例书')
shown = build_structured_profile([fact], subject='user')['habits'][0]['text']
print('中文展示字段:', display_of(fact))
print('习惯卡片文本:', shown)
assert shown == 'user weekly reads 示例书' and shown != display_of(fact)
'@ | python -
```

[返回事项](../../../docs/issues.md#issue-017)。
