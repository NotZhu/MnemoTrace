# 设计取舍

本页只解释仍有价值的选择与代价。当前行为见[架构](architecture/README.md)，未完成事项见[问题台账](issues.md)，项目规则只在[AGENTS](../AGENTS.md)维护。

## 事实与原文共同读取

事实提供结构、冲突及时间信号，原文补回抽取丢失的细节。采用 `provenance` 与替代链回源，并保留语义 chunk 份额，避免错误事实挤掉全部原文。摘要只作补充。早期来源提升的回归与修正见[读路径证据](../results/experiments/README.md#readpath)；离线接线成功不代表最终 QA 增益。

## 先判断便宜冲突

槽位、多值保护、偏好反转、内容包含及嵌入相似度先行，只有歧义可升级 LLM。明确替代采用 `invalid_at/expired_at` 与 `supersedes`，保留来源；用户断言与手动擦除分别处理。代价是复杂语义矛盾仍需独立验证，不能把不同 `object` 全当冲突。

## 复用 Fact 保存会话结论

`decision/finding/lesson/open_question` 复用 `Fact` 的来源、双时间与检索能力，避免在多个模块维护另一套对象。长句与 session id 不投影成实体边。代价是精确去重不解决同义结论，标准 benchmark 又不走 `close_session`，需要独立会话验收；见[结论证据](../results/engineering/README.md#prompts)。

## 快照、稳定锁与提交身份

内存 store 加 `JSONL/manifest` 保持核心零依赖，可选 LanceDB 仅承担向量能力。manifest 是发布点，提交令牌避免旧缓存覆盖新写入；目录外稳定锁保护加载、修改、保存和擦除。代价是裸 `Memory` 不等同于服务事务，LanceDB 热读也不能套用内存快路径。见[事务证据](../results/engineering/README.md#service-transactions)。

## 原始用户编号隔离

移除编号关联和旧 pickle 转换，读、写、审计及擦除精确匹配原始 `user_id`。姓名/昵称共指保留在同用户范围内。旧关联快照明确拒绝，旧目录不自动认领，避免静默改变历史可见范围。历史 JSONL 和上下文等价验证见[归属证据](../results/engineering/README.md#service-transactions)。

## 凭据与内容采用不同边界

原始 `Key` 不在浏览器持久存储；接入配置只引用用户私有文件，Windows 显式设置 owner 与 DACL。配置按 `JSON/TOML/shell` 分别编码，doctor 仅脱敏报告内容，固定字段和状态保持原义。代价是刷新需重新登录、凭据仍是权限保护明文，旧历史和服务端 `key` 需独立处置。见[安全证据](../results/engineering/README.md#agent-credentials)。

## 查询库独占页面链提交

统一 Infinite Query 处理重载与已加载深度，写后显式取消再失效，按登录周期、提交版本和评估时刻隔离。全部成功再替换，失败保留旧页和草稿。代价是重载请求量随深度增加，不提供历史快照或持续写入自动追赶；见[分页证据](../results/engineering/README.md#console-lifecycle)。

## 图谱展示采用 Quartz 适配

采用 `D3`/`PixiJS` 替代自写布局，显示与模拟分离，内存缓存视角并明确资源释放。MIT 来源及固定上游版本见[适配说明](../frontend/vendor/quartz/README.md)。视觉尺寸和受力不参与 `salience`/检索，布局不承诺零交叉；早期自写布局不作为当前实现。

## 未采用或收益未证实

| 方向 | 当前取舍与证据边界 |
| --- | --- |
| 中文阈值实体归一化 | 误伤英文实体和图边，已回滚；[历史对照](../results/experiments/README.md#entity-normalization)不否定一般归一化方案 |
| `aggregation_pool_boost` 的独立收益 | 当前仍开启；合包改善未在纯 multi-session 切片复现，成本增加不能当作已证明收益；[读路径实验](../results/experiments/README.md#readpath) |
| 仅换更强 answerer | 有限探索没有稳定增益，延迟增加；不构成 benchmark 的结构性上限 |
| TIME SPANS / 时间措辞保留 | T1 目标类别未证实收益并回滚；T2 的 prompt 改变使原实验不可归因；[时间实验](../results/experiments/README.md#temporal) |
| 清空时先释放锁或自写递归删除 | 用稳定目录外锁与 rmtree 替代，避免锁身份分裂、写入窗口及链接目标遍历 |
| 旧页面错误重建、持久化凭据与手写分页缓存 | 统一当前登录/页面/查询生命周期，旧工程截图不证明当前方案的全部边界 |

## 外部参考与独立实现

外部项目用于理解模式和提出可消融候选，不构成 MnemoTrace 已具备对应能力或质量。来源声明、论文结果、本地复现和本框架结果必须区分；设计不得以不同 harness 数字建立同条件比较。

复制代码需保留适用许可与来源；观察思想后独立实现不等于已获得所有许可结论。当前实际采用的 Quartz 代码以其适配说明和 MIT 许可证为准。未采纳的外部方案与长期参考清单见[历史来源与许可审查](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/specs/002-memory-reference-radar/research.md)，候选分析见[历史技术报告](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/specs/002-memory-reference-radar/technical-report.zh-CN.md)。这些快照保留当时的审查状态，不能当作当前许可证或性能保证。
