# 画像来源数量与来源状态独立展示验证

- 日期：2026-10-14
- 范围：用户已审阅并确认的计数修复。
- 不涉及事实重新提取、真实记忆回写、模型调用或 benchmark。

## 最终行为

结构化画像条目的 `evidence` 新增 `source_count`，从 `Fact.provenance` 的去重 ID 数量计算。
保留原 `kind/count`；用户设定固定计数、普通提及计数及访问强化次数的旧语义不变。
前端来源数大于 0 时显示 `xN`（14px）；手动设定只显示带悬浮提示的 UserCheck 图标，已强化状态独立显示。
没有来源的手动新增事实、
没有对应事实的身份补全不显示虚构的 ×1。

新字段只在画像读取时派生，没有增加 Fact 持久化字段或迁移已有记录。
接口与前端应同步部署；本次已只读确认运行中的后端返回新字段，且用户设定条目可保留正来源数。

## 回归验证

修复前先运行新增的 `tests/test_profile_evidence.py`，6 项均失败：响应缺少 `source_count`。
修改实现后，新回归与现有事实编辑回归全部通过。

在仓库根目录运行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_profile_evidence.py tests/test_fact_metadata_updates.py -q -p no:cacheprovider
.venv/Scripts/python.exe -m pytest tests/test_lean.py::test_structured_profile_tiers_display_only_and_preserves_recall tests/test_lean.py::test_structured_profile_includes_cold_tier_facts -q -p no:cacheprovider
.venv/Scripts/python.exe -m pytest tests/test_server.py::test_structured_profile_source_count_survives_edit_and_reload -q -p no:cacheprovider -o faulthandler_timeout=30
```

结果分别为 27、2、1 项通过。接口测试使用临时目录、离线提取器和合成用户，验证
`remember → structured profile → edit → service reload → structured profile` 后来源数仍为 1，
状态变为 user；直接手动新增没有来源的条目返回 0。沙箱中的 TestClient 未返回，已停止该测试
进程并在沙箱外重跑成功；没有停止项目服务。

在 `frontend` 目录运行：

```powershell
& 'D:/Program Files/nodejs/node.exe' --test tests/profileEvidence.test.cjs
& 'D:/Program Files/nodejs/node.exe' node_modules/typescript/bin/tsc -b
& 'D:/Program Files/nodejs/node.exe' node_modules/vite/bin/vite.js build
```

5 项 React 服务端渲染检查通过，验证实际组件输出：

| 条目 | 界面输出 |
|---|---|
| 自动提取，2 条来源 | x2 |
| 用户设定，2 条来源，旧 count 为 1 | x2 与带悬浮提示的 UserCheck 图标，没有手动设定正文标签 |
| 访问强化 7 次，1 条来源 | x1 已强化，不显示 x7 |
| 手动新增，无来源，旧 count 为 1 | 仅带悬浮提示的 UserCheck 图标 |
| 身份补全，无对应事实 | 不显示来源计数或状态 |

类型检查与 Vite 构建通过。测试使用 esbuild 的自动 JSX 转换；初次测试脚本遗漏该选项导致
React 标识未定义，修正测试加载方式后全部通过。Vite 在沙箱中受 realpath 权限限制，沿用本地
沙箱外构建完成验证。

## 验证边界

本次前端验证为实际组件的服务端渲染检查与生产构建，没有进行新的浏览器截图验收。
`xN` 表示关联的去重来源 Episode ID 数量，不是行为发生频率，也不验证这些原始记录当前是否仍可读取。
分类、重复总结、候选入口和语言展示事项仍按[已知错误文档](../docs/known-issues.zh-CN.md)中的对应记录处理；编号已按严重程度重新整理。
