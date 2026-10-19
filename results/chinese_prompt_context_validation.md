# 中文默认提示词与证据分区验收（2026-10-16）

## 变更范围

- 三个内置 system prompt 和调用引导改为中文，摘要、叙事画像明确要求简体中文；画像采用四个中文内部小节。
- 提取仍使用英文 JSON 字段和 predicate，subject/object/text 的语言规则保持跟随原文；无外部服务的离线回退继续可用。
- 偏好表标题和列名在后端生成中文，证据预算分类同时识别新旧标题。
- 前端识别新中文与旧英文顶层证据块，STYLE 和中文画像内部小节不再拆卡；复制保留原上下文。
- 默认值按现有 policy 入口下发，自定义覆盖、策略存取和画像缓存清除沿用原规则。未处理召回范围或跨层去重。

## 自动验证

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$promptTestDirectory = Join-Path (Get-Location).Path ('.pytest_cache/prompt-language-' + [guid]::NewGuid().ToString('N'))
python -m pytest tests/test_prompt_language.py tests/test_lean.py tests/test_product_fixes.py tests/test_outcomes.py -q --basetemp $promptTestDirectory
```

180 项后端测试通过，包含默认指令传递、英文关系和原文值保留、自定义策略持久化与画像缓存刷新、
中文偏好表和新旧标题预算分类。关闭第三方 pytest 插件时出现两条已有 asyncio 配置项警告。
初次使用系统临时目录的测试因 sandbox 临时目录权限错误中断，换用工作区内的新临时目录后通过。

```powershell
cd frontend
node --test tests/contextSections.test.cjs
npm run build
```

4 项分区回归和 TypeScript/Vite 构建通过。分区覆盖中文四小节、旧英文 STYLE、中文偏好表、
其他英文证据类型、Windows 换行及原始正文保留。`python examples/quickstart.py` 离线示例通过。

## 浏览器验证与限制

临时无界面 Edge 探针加载真实 Ask 页面与生产 CSS，仅替换查询和剪贴板为合成数据。
1280 × 900 与 390 × 900 下，中文例子显示 3 张预期证据卡；旧英文例子显示 2 张，STYLE 留在画像内。
两种场景复制均与原始上下文完全一致，无横向溢出或页面脚本异常。探针结束后移除。

模型调用测试使用记录参数并返回预设内容的替身，不代表真实模型一定遵守中文指令。
未调用真实外部模型、未重跑 benchmark，也未更改现有 benchmark 数字；未读取或重处理真实记忆。
默认值升级需要运行中的后端加载新代码；已有自定义提示词需按用户意愿另行更新，历史摘要不会自动改写。
