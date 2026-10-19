# 关系图谱搜索输入节奏验收

日期：2026-10-14。仅验证前端交互，使用合成数据和模拟时钟，无真实记忆请求。

原实现每次输入都将 q 直接交给 `useGraph`，加载时整页提前返回 Spinner，导致自动搜索
过于频繁并可能卸载输入框。复用现有 `useDebouncedSearch`，等待 500ms 输入停顿、暂停中文
组词期间的提交，清空立即恢复；工具栏持续挂载，加载/错误只替换结果区。

新增 `frontend/tests/graphSearch.test.cjs` 通过实际 GraphPage 的输入事件和实际防抖 hook
验证提交到 useGraph 的词，模拟计时避免真实等待。修改前 3 项分别失败：立即提交 Bei、
缺少组词事件处理、加载时输入框消失。修改后 3 项全部通过。

相关搜索回归共 **6 项通过**，前端 TypeScript 检查与 Vite 构建通过：

```powershell
cd frontend
npm run test:search
npm run build
```

此次调整不改 API、后端检索或图谱布局，不构成 benchmark 或性能测量。
