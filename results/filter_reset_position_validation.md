# 筛选重置位置与文案验收

日期：2026-10-17。

- 会话结论与事实管理的重置入口移至页面内容区底部中央，统一文案为“重置筛选”，保留 × 图标和主题描边。
- 空结果主体仍以页面内容区为基准居中；`PageEmptyState` 增加可选底部操作区并预留间距，较矮页面允许自然滚动以避免重叠。
- 会话结论清空搜索和类型筛选，保留历史版本开关；事实管理清空搜索和分类、恢复当前有效状态，保留敏感内容设置。
- 保留原显示条件：事实管理仅在筛选后无结果时提供入口；会话结论在存在搜索或类型条件时提供入口。

## 验证

在 `frontend/` 执行：

```powershell
node --test tests/journalSearch.test.cjs tests/factsFilters.test.cjs tests/filterSelect.test.cjs tests/preferences.test.cjs tests/searchQueries.test.cjs
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build
```

23 项测试、类型检查及生产构建通过。回归覆盖重置入口所在的底部操作区、单个入口、搜索和类型重置、事实筛选组合及偏好保留。

Edge 实测两个页面：1528 × 732 视口下，按钮水平中心均为内容区中心 x=892，底部 y=700，距视口底部 32px，
与主内容底部内边距一致；文档高度均为 732px，无额外空白滚动。点击后搜索清空、原列表恢复。
浏览器仅输入合成搜索词并重置查询，未新增、编辑、删除或总结真实记录。未修改 API、记忆处理算法或持久化数据。
