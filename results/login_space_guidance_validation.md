# 登录空间引导验收

日期：2026-10-17。

- 开放模式提示相同 Key 可返回同一空间；输入框提示“输入已有空间的 Key”。
- 快捷按钮使用“生成新的空间 Key”“填入演示空间 Key”。
- 按用户要求移除生成后显示的保存提示小字、对应样式及临时状态；返回原空间的说明保留在卡片顶部。
- 生成后仍可手动修改、清空或填入演示 Key。快捷按钮不自动登录、不创建服务端空间。
- 受保护模式说明使用管理员提供的 API Key。两个快捷按钮继续显示并禁用；检测中、禁用或服务不可用时同样禁用。

验证命令（`frontend/`）：

```powershell
node --test tests/demoAuthMigration.test.cjs tests/apiKeyValidation.test.cjs tests/loginGuidance.test.cjs
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build
```

11 项相关测试通过，TypeScript 检查及生产构建通过。无真实记忆写入、鉴权配置修改或 benchmark 运行。
