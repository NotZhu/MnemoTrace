# 实施计划

基线 bc163abd640cb62e7a84702fee1e87f02e35948a；分支 fix/console-pagination；全部操作在 E:/MnemoTrace-worktrees/console-pagination。

1. 修订规格、任务和一致性门禁，再逐个测试接缝建立失败回归并保存输出。
2. 共享分页移除第二缓存写入者，使用 observer refetch 重建完整已加载链，接入 AbortSignal；以真实 QueryClient 和生产 staleTime=10000 检查取消与原子提交。
3. MemoryService/HTTP 加入成对浏览上下文与有限时间校验，所有事实状态和计数使用同一时刻；更新既有版本回归以携带上下文。
4. 同步 API.md 与 TypeScript SDK 的 outcomeKind/expectedRevision/evaluatedAt、响应字段及错误契约，运行 SDK 测试与类型检查。
5. 独立合成服务和生产构建验收四页、错误、版本冲突、编辑草稿和展开状态；完整 Python/前端/SDK/zero-setup 检查。
6. 按模块更新架构、台账及 results 证据。保留台账独立调整，扫描命名与空白；全部验收后评估提交就绪。

测试接缝：真实 InfiniteQueryObserver/QueryClient 的查询结果与请求、MemoryService 公共浏览接口、HTTP 及 SDK 请求/响应、真实生产页面。用户已在实施计划中确认这些接缝；无需再次确认。
输入边界：浏览参数不可信，服务与 HTTP 均校验；既有鉴权/空间隔离不变，测试仅合成数据，不新增敏感字段或依赖。

首屏写入补充：复现无 data 的 Query.fetch 去重，再统一写后取消/失效入口。取消屏障覆盖同一写入影响的查询根，等待后检查登录周期；手动重载仍由 Infinite Query 管理。测试覆盖无缓存首屏、新筛选首屏、单次概览、冲突/策略旁路、连续写入及登录切换。保留写入成功回调现有响应时机，不等待后台刷新完成。
