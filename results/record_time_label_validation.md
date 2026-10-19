# 原始记录时间标签与相对时间提示词验证

日期：2026-10-13。按用户逐项确认的时间方案实施；此前已撤回的身份筛选和代码日期解析
方案没有重新实施。全部验证使用独立合成记录，没有读取真实用户记忆。

## 修改范围

- `mnemotrace/memory.py::Memory.lean_context` 的完整原始片段输出“原始记录时间 / 会话 /
  记录内容”。日期来源顺序和原文保持原实现。
- `mnemotrace/service.py::_ANSWER_SYSTEM` 的时间说明替换为：
  “原始记录时间表示内容被记录的时间，不等同于事件发生时间。”
  “根据原文判断事件时间；相对时间以对应的原始记录时间为参照推算，保留原文的时间精度。”
- 没有在代码中换算相对日期，也没有更改结构化事实的时间字段。

## 离线验证

模拟 answerer 接收真实 lean_context 的合成原始记录。验证了日期元数据优先、缺少元数据
时使用记录自身时间、原文完整保留、新 system 规则与上下文一同传入、回答去除首尾空白。
实际命令输出：

```text
PASS: date fallback, imported date precedence, unchanged source text, and time-reference system prompt reach answerer.
```

回归命令（PowerShell，每次使用新的临时目录）：

```powershell
$taskBase = Join-Path (Get-Location).Path ('.mnemotrace/dev/pytest-time-label-' + [guid]::NewGuid().ToString('N'))
if (-not ([IO.Path]::GetFullPath($taskBase)).StartsWith('E:\MnemoTrace\.mnemotrace\dev\')) { throw 'Unexpected test path' }
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_lean.py tests/test_service_paths.py -ra --basetemp $taskBase
```

结果：**154 passed in 3.61s**。原始日志：[record_time_label_pytest.txt](record_time_label_pytest.txt)。

## 模型定向检查

使用独立内存对象、hashing embedder、合成手动事实及来源；仅在答案生成阶段调用已配置
回答模型。没有代码侧时间解析，模型拿到完整原文、原始记录时间和原有结构化事实。

| 原始记录日期 | 合成原文 | 实际模型回答 |
| --- | --- | --- |
| 2026-10-12 | 昨天晚上做了实验A。 | 实验A是2026年10月11日晚上做的。 |
| 2026-10-20 | 上周做了实验B。 | 实验B是在2026年10月12日至10月18日那一周做的（原始记录时间为2026年10月20日，记录中说“上周做了实验B”）。 |

完整合成上下文与回答：[record_time_label_synthetic_live.jsonl](record_time_label_synthetic_live.jsonl)。
本次为两个定向示例的检查，不是 benchmark，也不保证模型对所有模糊或引用时间表达都
正确推断。脱敏模式仍沿用原有自由文本排除行为，没有原文时无法凭标签恢复相对时间。
