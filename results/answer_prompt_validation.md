# 记忆问答提示词验收

日期：2026-10-13。

`mnemotrace/service.py::_ANSWER_SYSTEM` 已替换为用户确认的四句通用规则。
使用模拟 answerer 验证 `_answer_from_memory` 将新 system 提示词、记忆上下文和问题完整传入，
并正常返回去除首尾空白的回答。没有调用外部模型或读取真实记忆。

复现命令（PowerShell）：

```powershell
@'
from types import SimpleNamespace
from mnemotrace.service import _ANSWER_SYSTEM, _answer_from_memory
calls = {}
model = SimpleNamespace(complete=lambda prompt, system: calls.update(prompt=prompt, system=system) or '  test answer  ')
assert _answer_from_memory(model, 'test question', 'test memory') == 'test answer'
assert calls['system'] == _ANSWER_SYSTEM
assert calls['prompt'] == '【记忆】\ntest memory\n\n【问题】test question'
print('PASS: revised prompt reaches answerer')
'@ | .\.venv\Scripts\python.exe -X utf8 -
```

本次实际调用验证结果：

```text
PASS: answerer receives revised system prompt, memory, and query; returned answer remains usable
```

退出码为 0。本记录仅验证提示词接线，不是实际模型回答准确率或 benchmark 结果。
