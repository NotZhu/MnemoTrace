# 摘要跟随原对话主要语言验收

日期：2026-10-18。范围：L2 会话摘要的默认语言指令及调用链路；不是模型 benchmark。

## 改动

按所有者要求，将 `mnemotrace/consolidate/summarizer.py` 中的默认指令：

> 使用简体中文撰写摘要，即使原对话是其他语言

改为：

> 使用与原对话相同的主要语言撰写摘要。

专有名称、准确数值和需逐字保留的原文值仍要求保持原样。提示词本身继续用中文书写；画像的
简体中文规则保持原样。没有新增语言检测服务、模型调用或存储字段。

新规则通过已有 policy 默认值传入摘要器。用户自定义 `summary_system` 仍优先；已有摘要不会
自动重写，`Memory.summarize_episodes()` 仍只处理没有摘要的 Episode。原始内容、离线截断回退、
策略存取与缓存刷新沿用原实现，没有修改真实记忆数据。

## 验证

更新原有提示词回归：检查实际传给摘要 LLM 的指令要求跟随原语言、不再要求跨语言输入强制
中文，输入原文和精确值保留要求仍传入；policy 暴露相同默认值，自定义覆盖与持久化仍有效。
LLM 使用固定合成回复，测试不连接真实模型。

在仓库根目录以 PowerShell 运行，临时目录选在可写工作区，避免当前沙箱系统临时目录限制：

```powershell
$summaryTemp = Join-Path (Get-Location).Path '.pytest_cache/summary-source-language-runtime'
New-Item -ItemType Directory -Force -Path $summaryTemp | Out-Null
$env:TEMP = $summaryTemp
$env:TMP = $summaryTemp
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest tests/test_prompt_language.py tests/test_lean.py tests/test_product_fixes.py tests/test_outcomes.py -o addopts='' -q --tb=short --basetemp .pytest_cache/summary-source-language-validation
```

结果：**180 passed, 2 warnings**。两条 warning 来自禁用第三方插件自动加载后，已有 asyncio
配置项没有被插件注册。原始测试输出见 [pytest 日志](summary_source_language_pytest.txt)，
个人用户目录前缀已替换为 `<USER_HOME>`。

## 结论边界

已取消默认摘要指令的强制中文要求。回归支持指令传递、原文保留、策略覆盖及现有摘要生命周期，
不证明真实模型一定遵循语言要求，也不证明检索或回答准确率改善。没有重跑完整模型基准、完整
pytest 或在线供应商验收；C-05 更新为“已取消摘要强制中文，质量影响待评测”。历史 benchmark
日志、分数与对应配置保持原样。
