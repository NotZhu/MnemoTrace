# 人物与关系归属提示词试验

日期：2026-10-13。用户确认先只补提示词。全部验证使用独立合成记录，没有读取真实用户
记忆，也没有重新实施已撤回的画像、人物分组和检索过滤方案。

## 修改

仅在 `mnemotrace/service.py::_ANSWER_SYSTEM` 第一条规则后增加：

```python
"准确区分人物及其关系归属；不同称呼只有明确的身份依据时，才可视为同一人。"
"回答前核对记忆中的主语及关系是否对应问题；缺少对应依据时说明信息不足，不借用其他人物的信息。"
```

现有时间规则与回答函数保持原实现。未修改画像、检索、身份别名、存储或记忆内容。

## 离线检查

模拟 answerer 验证两句身份规则和此前的时间规则一起传入 system；问题与记忆上下文
原样传入，回答去除首尾空白。命令实际输出：

```text
PASS: approved identity rules and existing time rules reach answerer.
```

服务回归命令（PowerShell，每次使用新的临时目录）：

```powershell
$taskBase = Join-Path (Get-Location).Path ('.mnemotrace/dev/pytest-identity-prompt-' + [guid]::NewGuid().ToString('N'))
if (-not ([IO.Path]::GetFullPath($taskBase)).StartsWith('E:\MnemoTrace\.mnemotrace\dev\')) { throw 'Unexpected test path' }
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_service_paths.py -ra --basetemp $taskBase
```

结果：**25 passed in 1.93s**。原始日志：[identity_prompt_pytest.txt](identity_prompt_pytest.txt)。

## 合成模型检查

每例创建独立 `Memory()`、hashing embedder 和手动合成事实，将 `lean_context` 原样交给
已配置回答模型。没有额外增加本人身份标签，也没有代码侧人物筛选。

| 场景 | 实际回答 | 判断 |
| --- | --- | --- |
| 仅 sample_person 住在示例城市，问本人住在哪；脱敏视图 | 你住在示例城市。 | 失败：无身份对应依据，仍混淆人物 |
| 仅 other_person 的母亲住在示例城市，问本人母亲；完整视图 | 无法回答本人母亲住址，明确指出记录说的是 other_person 的母亲 | 本例正确区分关系归属 |
| 点名问 other_person 的母亲 | other_person的母亲住在示例城市。 | 本例正确 |
| 明确 user 的母亲为 Mother_A，且 Mother_A 住在示例城市 | 你母亲Mother_A住在示例城市。 | 本例正确 |
| 明确 user 的姓名为 sample_person，且其住在示例城市 | 你住在示例城市。 | 本例正确 |

完整合成资料及模型回答：[identity_prompt_synthetic_live.jsonl](identity_prompt_synthetic_live.jsonl)。

## 结论与范围

亲属示例在本次调用中得到正确回答，明确身份/关系下的回答仍可生成；未绑定人物住址
仍发生误答，不能宣称提示词已彻底修复人物混淆。默认脱敏场景没有画像/原文，因此该例
误答也说明模型缺少明确本人身份参照时，单靠通用规则仍可能擅自对应人物。
这是五个定向样例的试验，不是 benchmark，也没有准确率泛化结论。
