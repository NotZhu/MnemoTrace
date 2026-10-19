# 事实分类与状态筛选验收

日期：2026-10-17。

## 最终行为

- 移除“包含历史记录”开关；“隐藏敏感内容”左侧依次显示分类与状态，默认全部分类、当前有效。
- 触发器只保留文字、箭头，无边框、无下划线；展开菜单使用主题表面、选中高亮与勾选，不再使用系统原生菜单。
- 分类选项取自当前空间可见的同类事实，不受当前分页、搜索或状态筛选限制。空分类归入“其他”。
- 分类、当前/历史状态、搜索词和敏感开关在后端共同作用，先筛选后分页；条数使用筛选后的总数。
- 旧历史布尔偏好迁移为全部/当前；新分类和三态状态继续按空间隔离保存。无结果时可清除分类、状态及搜索，保留敏感设置。

## 自动验证

仓库根目录：

```powershell
python -m pytest tests/test_server.py -q
```

43 项 HTTP 回归通过。新增测试覆盖冷热存储、分类与状态及搜索组合、分页、空分类、旧分类、空结果、
会话结论排除、敏感分类可见性与空间隔离，且分类条件不影响原始对话。
受限执行环境中 TestClient 停滞，终止后在本机权限下通过；全程使用测试 fixture 的临时数据与离线配置。
环境另有两项 pytest asyncio 配置未识别警告，未导致同步接口回归失败。

`frontend/`：

```powershell
node --test tests/factsFilters.test.cjs tests/filterSelect.test.cjs tests/preferences.test.cjs tests/searchQueries.test.cjs
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build
```

19 项前端回归、TypeScript 检查及生产构建通过。覆盖控件顺序、无原历史开关、主题菜单与入口样式、
鼠标与键盘选择、取消、失焦、分页重置、清除筛选、偏好迁移与空间隔离。

## 浏览器核对

Edge 在 `http://127.0.0.1:5173/ui/facts` 检查：最终触发器 `border-width: 0px`、透明背景与透明 outline，
`text-decoration: none`；展开菜单使用主题变量对应的表面与文本色。实际分类候选可完整返回，
选择历史状态后显示历史记录；核对后恢复原状态。只切换查询与展示偏好，未新增、编辑或删除真实记忆。

后端已按原配置重新启动以加载接口变更，继续仅监听本机。未调整分类算法、事实正文、来源或双时间历史。
