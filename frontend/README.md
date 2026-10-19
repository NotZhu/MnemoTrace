# MnemoTrace 记忆控制台 (Memory Console)

A production-grade web UI for managing your MnemoTrace long-term memory — view, ask, edit,
customize, and export everything the engine remembers about you.

> 这是给"小白"也能看懂、也能自己管理记忆的前端：查看用户画像、增删改记忆、定制关注点、
> 可视化时间线与关系图谱、一键导出或清空。它把 MnemoTrace 的可审计记忆能力做成一个
> **完全开源**的管理界面。

## Stack

| Concern | Choice |
|---|---|
| Framework | React 18 + TypeScript |
| Build | Vite 5 |
| Styling | Tailwind CSS 3 (brand design tokens in `tailwind.config.ts`) |
| Routing | React Router 6 |
| Server state | TanStack Query 5 |
| Auth/UI state | Zustand (API key in page memory only) |
| Icons | lucide-react |

The knowledge-graph view uses a Quartz-derived D3 force simulation and PixiJS renderer, loaded on demand.
The React wrapper is `src/components/ForceGraph.tsx`; source provenance and MIT attribution are recorded in
[`vendor/quartz/README.md`](vendor/quartz/README.md). The backend API and memory data model are unchanged.

## 界面文案

登录页和管理控制台固定使用中文，文案集中在 `src/lib/copy.ts`，组件及 API 错误提示直接读取
该模块。新增文案时修改对应分组，含计数等参数的文案可使用函数。
界面不检测浏览器语言，不读写语言偏好，也不渲染双语占位；技术名称与用户记忆正文保留原文。

## 登录凭证

登录 key 仅保存在当前页面的内存中，不写入 localStorage 或 sessionStorage，刷新或重新打开页面
后需要重新输入。随机生成的空间 key 需要自行保存，以便再次进入同一空间。
启动时尝试删除旧的 `localStorage['mnemotrace.auth']`，不读取或恢复旧凭证；包含凭证的查询
错误标识也仅保存在内存，并清除旧的 `sessionStorage['mnemotrace.query-errors']`。
存储访问被浏览器限制时仍可使用内存登录。主题和下述空间偏好继续持久化，空间偏好使用指纹，
不保存明文 key。验证见 [凭证内存态验收](../results/auth_memory_only_validation.md)。

## 界面偏好

`src/store/preferences.ts` 用 Zustand 管理浏览器长期偏好，版本化保存在 `mnemotrace.preferences`。
图谱显示和作用力参数为当前浏览器通用；图谱/事实过滤、结论历史开关、问答片段数及敏感信息处理选项
按 API 地址与登录凭据的 SHA-256 指纹隔离，与现有查询缓存的账号隔离边界一致。
同一后端空间更换凭据后不会自动合并旧偏好；此阶段不新增服务端身份或跨设备同步接口。

开关与问答选项立即保存；图谱滑条即时更新，磁盘写入合并 150ms，离开图谱或触发 `pagehide` 时补存。
图谱重置同步保存默认值。读缓存时校验字段、类型、范围和版本；存储失败时继续使用内存状态，
Web Crypto 缺失或 digest 报错时，使用 `@noble/hashes` 的 JavaScript SHA-256 计算相同指纹，
保持原有缓存格式；只有两种计算路径都失败时才使用不落盘的临时空间。
同一浏览器来源中，刷新后重新输入同一 key 即可恢复对应偏好；HTTP 与 HTTPS 的浏览器存储仍各自独立。
搜索、草稿、问答结果、弹窗及选中节点不进入长期缓存。兼容回退见
[SHA-256 偏好验收](../results/preferences_sha256_fallback_validation.md)。
图谱节点位置和视角另外使用有界内存缓存：同次使用期间切页返回以缓存位置为起点，播放与首次进入相同的 D3 自然展开，缩放和平移保持。节点会按当前受力继续运动并稳定，刷新后重新布局。减少动画偏好下直接显示。
“仅当前”只控制同一完整布局中的节点/关系显隐，保持相同坐标、受力和相机；统计、详情及键盘列表跟随可见内容。搜索、敏感过滤和返回数量限制仍作用于完整查询；显隐验收见 `../results/graph_visibility_validation.md`。
“适应画布”按当前设置重新排列、居中并展开，保留设置/筛选/选择；“重置图谱”恢复默认设置、清除选择后执行相同流程。两个按钮先按预计算轨迹的终点适配，再播放真实 D3 展开轨迹，结束不再补适配；见 `../results/graph_reset_fit_validation.md`。
复现与浏览器检查见 `../results/ui_preferences_validation.md`。

## 明暗主题

登录页和内部顶栏提供太阳/月亮切换按钮。默认深色，选择保存在浏览器的 `mnemotrace.theme`，
刷新和重新打开页面时恢复；存储不可用时仍能在当前页面切换。
`src/index.css` 定义两套蓝灰色变量，`tailwind.config.ts` 将共享组件及历史 slate 文本类映射到变量。
浅色使用浅灰背景、白色卡片、深蓝灰文字、淡蓝图标底色及按钮；登录图谱与粒子同步适配。
主题只改变前端展示，不影响认证、数据或服务调用。
手动切换时，页面、卡片、按钮与滑块底色同步渐变 200ms；临时过渡结束后恢复普通交互样式。
登录页使用 `src/lib/themeReveal.ts`，新主题从开关中心以圆形扩散覆盖页面（480ms）；内部页面继续渐变。
开关尺寸为 `72 × 36px`；浏览器不支持原生视图过渡时，登录页回退为普通主题切换。
首次加载不启用渐变，系统开启减少动态效果时跳过新增的颜色过渡。
验证：`npm run test:theme`、`npm run build`；完整验收见 `../results/theme_validation.md`。

## Views

- **总览 Dashboard** — stats, L3 persona, quick "remember".
- **记忆问答 Ask** — retrieve a *lean* context and see how few tokens answer a question (Bet A/E).
- **事实管理 Facts** — full bi-temporal CRUD; user edits are authoritative (🔒, never auto-overwritten).
- **时间线 Timeline** — chronological evolution with supersession (live vs. replaced).
- **关系图谱 Graph** — entities + relations; solid = current, dashed = invalidated.
- **关注点 Focus** — track (salience boost) / mute (hide) topics. Real wiring, not cosmetic.
- **原始对话 Conversations** — the cross-agent lifecycle surface: choose a `session_id`, see
  content-free `agent_status`, write with `scope=auto|long|working`, close the session, and audit what
  durable facts that session saved. It also includes a content-free session index so users can see which
  Codex/Claude/app sessions touched their memory.
- **隐私与数据 Privacy** — safe export by default (non-sensitive facts + graph), explicit private export,
  and confirmed erase (right to be forgotten).

## Product Lifecycle

For a C-end product or an agent console, the first-class workflow is:

```text
Open a conversation/thread
  -> call agent_status(session_id) to confirm namespace + session wiring
  -> list /v1/sessions when the user needs a cross-agent session index
  -> recall before answering user/project-history-dependent tasks
  -> remember durable facts with scope="auto" or scope="long"
  -> remember temporary task state with scope="working"
  -> close_session(session_id) when the thread ends or switches tasks
  -> show session_report(session_id) so the user can audit what was saved
```

The API key is the user's memory namespace. `session_id` is provenance and lifecycle scope, not a
separate memory silo; Codex, Claude Code, Cursor, and your app should reuse the same namespace when the
user expects one shared memory layer.

## Develop

使用 Node 22，与 CI / Docker 保持一致；当前 SHA-256 依赖要求 Node >=20.19。包管理器版本由 `packageManager` 固定。

```bash
pnpm install
# run the memory server in another terminal (serves the API on :8000)
#   MNEMOTRACE_OPEN=1 MNEMOTRACE_EMBEDDER=bge-small uvicorn mnemotrace.server.app:app --port 8000
pnpm dev            # Vite dev server on http://localhost:5173/ui/ (proxies /v1 + /health to :8000)
```

## Build (served by the Python server)

```bash
pnpm build          # → frontend/dist
```

The FastAPI memory server (`mnemotrace/server/app.py`) auto-detects `frontend/dist` and serves the
console at **`/ui`** (with SPA fallback so deep links survive a refresh); `/` redirects there.
When `dist/` isn't built, `/` and `/ui` show build instructions with HTTP 503. Build the console
and restart the server to enable it. The HTTP API, health checks and offline quickstart remain
usable without a Node build step; the inline dashboard has been removed.

Configure a non-same-origin API with `VITE_API_BASE` (see `.env.example`).
