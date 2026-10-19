# 图谱生产 CSP 兼容验收（2026-10-18）

## 修复范围

仅修复审查中的第二个问题：PixiJS 默认动态代码生成与生产 CSP 冲突。
在 `frontend/src/vendor/quartz/graph.ts` 加载 `pixi.js/unsafe-eval` 静态兼容模块，
不增加依赖、不修改服务端 CSP。图谱仍按原路径动态加载，不调整查询、布局、交互或记忆算法。

## 失败复现与回归

新增回归位于 `frontend/tests/quartzGraph.test.cjs`。它将真实图谱入口与同一份 Pixi
依赖按浏览器目标压缩打包，再置于 `vm` 的 `codeGeneration.strings=false` 环境中执行。
测试先确认 `new Function` 被禁止，然后检查实际渲染器的初始化限制及实际 WebGL uniform
同步实现；仅 GPU 调用出口使用记录参数的替身，避免依赖本地 GPU。

在 `frontend/` 执行：

```powershell
node --test --test-name-pattern="生产依赖" tests/quartzGraph.test.cjs
```

修复前为 1 项失败，错误来自真实 Pixi 的 `AbstractRenderer._unsafeEvalCheck`：

```text
Current environment does not allow unsafe-eval, please use pixi.js/unsafe-eval module to enable support.
```

加入兼容模块后，该回归通过，且 uniform 的 `alpha` 位置收到预期值 `0.75`。
这同时验证模块副作用在压缩打包后仍保留，避免只绕过检查却未安装静态同步实现。

## 验收结果

```powershell
npm run test:graph
npm run build
```

- 图谱回归：25 项通过，0 失败，0 跳过。
- TypeScript/Vite 生产构建：通过。
- CI web job 增加 `pnpm run test:graph`，在依赖安装之后、生产构建之前执行。
- 服务端 `mnemotrace/server/app.py` 的 CSP 保持原样，没有加入 `'unsafe-eval'`。

本次验证覆盖受限 JavaScript 环境与真实依赖同步路径，未执行真实浏览器 GPU 渲染验收，
不据此声明浏览器或显卡兼容性范围。未调用外部模型、未读取真实记忆、未重跑 benchmark。
