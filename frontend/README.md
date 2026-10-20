# MnemoTrace 记忆控制台

用于管理 MnemoTrace 长期记忆的生产级 Web 界面：查看、提问、编辑、定制并导出引擎保存的关于你的记忆。

> 这是给"小白"也能看懂、也能自己管理记忆的前端：查看用户画像、增删改记忆、定制关注点、
> 可视化时间线与关系图谱、一键导出或清空。它把 MnemoTrace 的可审计记忆能力做成一个
> **完全开源**的管理界面。

## 技术栈

| 用途 | 选型 |
|---|---|
| 框架 | React 18 + TypeScript |
| 构建 | Vite 5 |
| 样式 | Tailwind CSS 3（品牌设计变量见 `tailwind.config.ts`） |
| 路由 | React Router 6 |
| 服务端状态 | TanStack Query 5 |
| 鉴权 / 界面状态 | Zustand（API key 仅保存在页面内存中） |
| 图标 | lucide-react |

知识图谱视图使用由 Quartz 适配而来的 D3 力模拟与 PixiJS 渲染器，按需加载。
React 封装位于 `src/components/ForceGraph.tsx`；来源与 MIT 署名记录见
[`vendor/quartz/README.md`](vendor/quartz/README.md)。后端 API 与记忆数据模型保持不变。

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

## 页面功能

- **总览 Dashboard** — 统计信息、L3 用户画像，以及快速记忆录入。
- **记忆问答 Ask** — 检索精简上下文，展示回答问题所需的少量 token（Bet A/E）。
- **事实管理 Facts** — 完整的双时间增删改查；用户编辑具有权威性（🔒，不会被自动覆盖）。
- **时间线 Timeline** — 按时间展示记忆演化与替代关系，区分当前有效和已被替代的内容。
- **关系图谱 Graph** — 展示实体与关系；实线表示当前有效，虚线表示已失效。
- **关注点 Focus** — 跟踪主题以提升显著度，或静音主题以隐藏内容；这些选项实际影响记忆处理。
- **原始对话 Conversations** — 跨智能体的会话生命周期入口：选择 `session_id`，查看不含记忆正文的
  `agent_status`，用 `scope=auto|long|working` 写入，关闭会话，并审计该会话保存的持久事实。
  还提供不含正文的会话索引，便于用户查看哪些 Codex / Claude / 应用会话访问过记忆。
- **隐私与数据 Privacy** — 默认安全导出非敏感事实与图谱；显式选择后可导出私密数据；确认后可清空数据，落实被遗忘权。

## 产品生命周期

面向个人用户的产品或智能体控制台，核心工作流如下：

```text
打开对话 / 线程
  -> 调用 agent_status(session_id)，确认命名空间与会话接入状态
  -> 用户需要跨智能体会话索引时，查询 /v1/sessions
  -> 回答依赖用户 / 项目历史的任务前，先 recall
  -> 用 scope="auto" 或 scope="long" 记住持久事实
  -> 用 scope="working" 记住临时任务状态
  -> 线程结束或切换任务时，调用 close_session(session_id)
  -> 展示 session_report(session_id)，供用户审计已保存的内容
```

API key 对应用户的记忆命名空间。`session_id` 用于来源追踪和生命周期管理，不划分独立的记忆空间。
如果用户期望共享同一层记忆，Codex、Claude Code、Cursor 和你的应用应复用同一命名空间。

## 本地开发

使用 Node 22，与 CI / Docker 保持一致；当前 SHA-256 依赖要求 Node >=20.19。包管理器版本由 `packageManager` 固定。

```bash
pnpm install
# 在另一个终端启动记忆服务（API 端口为 :8000）
#   MNEMOTRACE_OPEN=1 MNEMOTRACE_EMBEDDER=bge-small uvicorn mnemotrace.server.app:app --port 8000
pnpm dev            # Vite 开发服务：http://localhost:5173/ui/（将 /v1 与 /health 代理至 :8000）
```

## 构建（由 Python 服务提供页面）

```bash
pnpm build          # → frontend/dist
```

FastAPI 记忆服务（`mnemotrace/server/app.py`）自动检测 `frontend/dist`，在 **`/ui`** 提供控制台，
并通过 SPA 回退让深层链接在刷新后仍可访问；`/` 会重定向到该页面。
尚未构建 `dist/` 时，`/` 和 `/ui` 返回 HTTP 503 并展示构建说明。构建控制台后重启服务即可启用。
无需执行 Node 构建，HTTP API、健康检查和离线 quickstart 仍可使用；旧的内联仪表盘已移除。

连接不同源的 API 时，使用 `VITE_API_BASE` 配置地址（见 `.env.example`）。
