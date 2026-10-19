# test 与 1 普通空间策略验收（2026-10-16）

## 变更

- 前端隐私页和 HTTP `/v1/forget` 的受保护名单移除 `test` 和 `1`。
- 认证缓存升级保留 `test`，不再将其重定向为 `mnemotrace-test`。
- 演示入口保持 `mnemotrace-test`；`anonymous`、`mnemotrace-test` 的清空保护保持。
- 普通空间的显式清空确认要求保持。未对真实部署中的记忆执行清空。

## 已完成验证

1. 清空入口规则回归：2 项通过，覆盖 `test` 和 `1`。使用替代服务验证未确认时为 400、确认后调用原有清空服务，剩余两个保护名称确认后仍为 403。

   ```powershell
   $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
   python -u -m pytest tests/test_server.py::test_forget_policy_allows_regular_namespaces_with_confirmation -q -o faulthandler_timeout=15
   ```

   关闭第三方插件自动加载后，未加载的 pytest-asyncio 配置产生两条配置警告，不影响该同步测试。

2. 前端认证回归：7 项通过，覆盖旧版本 `test`、`1` 缓存原样恢复、其他 Key、无效 Key 和登录退出。

   ```powershell
   cd frontend
   node --test tests/demoAuthMigration.test.cjs tests/apiKeyValidation.test.cjs
   ```

3. TypeScript 与 Vite 生产构建通过。

   ```powershell
   cd frontend
   npm run build
   ```

## 验证限制

`tests/test_server.py` 全集及 `-k forget` 的 HTTP 集成运行均未完成；禁用第三方插件后可完成收集，
但首个使用 TestClient 的集成用例没有结束，已手动终止，具体阻塞原因尚未确认。
现有 HTTP 清空集成用例已增加 `test`、`1` 参数覆盖，本记录不将未完成的集成运行计为通过。
此变更不涉及 benchmark 数字。
