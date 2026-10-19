# 前端选择器复用与无调用导出清理验收

日期：2026-10-18。

- 删除经前端源码及测试引用搜索确认无调用的 `humanizePredicate`、`estimateTokens`、`useAddWorking`。
- `ScopeSelect`、`ExportRangeSelect` 改为 `FilterSelect` 的类型化封装，共用交互逻辑。保留原文案、选项值、布局样式、范围标签关联、导出说明关联及禁用行为。
- `FilterSelect` 的值与回调使用同一字符串泛型，封装层仍保留 `RememberScope` / `ExportMode` 类型约束。

在 `frontend/` 执行：

```powershell
node --test tests/*.test.cjs
npm run build
```

134 项前端测试全部通过，0 失败、0 跳过；原始测试输出见 [测试日志](frontend_select_cleanup_tests.txt)。
TypeScript 检查及 Vite 生产构建通过。
回归包含原有筛选行为、范围键盘选择与标签关联、导出范围禁用时关闭且不能选择、重新启用后恢复操作。

仅调整前端展示组件的实现边界。未修改记忆算法、服务/API 或持久化格式，未运行 Python 测试、真实浏览器验收或模型 benchmark。
