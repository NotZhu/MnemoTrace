# 已确认的会话结论提示词变更验收

日期：2026-10-13。

## 变更范围

应用用户审阅的三处代码改动，并采用最终确认的“不限制会话场景”开头：
`mnemotrace/consolidate/outcomes.py::OUTCOME_SYSTEM`、`frontend/src/lib/copy.ts`、
`frontend/src/pages/Journal.tsx`。没有加入已撤回的 `outcomes_status` 字段、服务层诊断或 SDK 改动。
15–400 字符过滤、JSON 解析、去重、异常处理、原始记录和存储方式保持原实现。
本轮未读取、外发或重新生成真实会话，模型检查仅使用下方日志中的合成输入。

## 离线检查

```powershell
.venv/Scripts/python.exe -m pytest tests/test_outcomes.py -q --basetemp .mnemotrace/pytest-session-prompt-approved
cd frontend
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build
```

31 项离线回归通过，原始输出在 `results/session_outcomes_prompt_pytest.txt`；TypeScript 检查和
前端构建通过。这些检查证明现有链路兼容，不证明真实模型能按新规则正确选择结论。

## 合成模型检查：仍存在未解决的零条结果

原始日志在 `results/session_outcomes_prompt_synthetic_live.jsonl`。使用现有配置的 `deepseek`，
temperature=0、max_tokens=1024、max_attempts=1、timeout=20 秒。每行保存完整 system 提示词、
合成输入与原始回复，没有真实会话或密钥。

| 合成场景 | 预期 | 实际回复 | 结果 |
| --- | --- | --- | --- |
| 已确定的半年摄影学习安排，混入一条普通个人属性 | 有结论 | `[]` | **失败** |
| 普通个人属性 | 无结论 | `[]` | 通过 |
| 临时要求缩短文字 | 无结论 | `[]` | 通过 |

三次模型调用均没有捕获到异常。这组结果证明，应用已确认提示词后仍可能漏掉明确决定；
不能声称已修复所有零条结果，也不能仅据这三个案例推导整体准确率。失败样本完整保留。
检查完成后未擅自追加其他提示词修改。

以下命令会仅向现有模型发送日志中的合成内容，重放日志中保存的提示词；失败案例仍有明确断言。

```powershell
@'
import json, os
from pathlib import Path
from mnemotrace.consolidate.outcomes import extract_outcomes
from mnemotrace.llm.providers import load_dotenv, make_llm
from mnemotrace.types import Episode
load_dotenv()
rows = [json.loads(line) for line in Path('results/session_outcomes_prompt_synthetic_live.jsonl').read_text(encoding='utf-8').splitlines()]
passed = True
for row in rows:
    llm = make_llm(row['model'], max_attempts=1, timeout=20)
    episodes = [Episode(content=text, user_id='synthetic-user', session_id='synthetic-session', event_time=i) for i, text in enumerate(row['input'])]
    facts = extract_outcomes(llm, episodes, 'synthetic-user', 'synthetic-session', system=row['system'])
    ok = bool(facts) == row['expected_any_outcome']
    print(row['scenario'], len(facts), ok)
    passed = passed and ok
assert passed, 'Synthetic outcome selection still fails'
'@ | .venv/Scripts/python.exe -
```
