# 记忆生命周期与存储

本页解释记忆的时间、来源、持久化与擦除边界。操作步骤见[使用指南](../guides/usage.md)，公共方法见 [Python API](../reference/python-api.md)。

## 数据模型

| 类型 | 用途与关键字段 |
| --- | --- |
| Episode | 原始内容，关联 user、session、speaker、event_time 与 ingested_at；摘要和整合标记是派生状态 |
| Fact | subject/predicate/object 断言，保留 provenance、source、有效时间、事务时间和 supersedes |
| Entity / Relation | 实体及由 fact 支撑的关系，用于图检索与可视化 |
| WorkingMemory | 带 session、TTL 和 consumed 状态的短时内容 |
| Conflict | 对疑似冲突的待裁决记录，关联新旧事实及处理状态 |

画像、摘要和图谱都是派生视图，不能代替原始来源。Fact 的 `provenance` 指向 Episode id，`source="user"` 标识用户明确添加或内容编辑的事实。

事实分类优先使用受支持的明确 predicate；未知关系只参考对象中的领域线索，不拆分未知关系猜测类别，主语和正文不提供类别证据。英文线索按完整单词或关系片段匹配，复合词中的空白、下划线和连字符等价；中文使用明确词语，单字线索只匹配独立值，无法判断时归“其他”。规则覆盖常见合成样例，不代表通用语义理解。新规则作用于新增事实、空分类事实及内容编辑后的重新分类；已有非空分类不会因读取、再次分类或仅修改元数据而自动重算。手动添加与编辑时显式传入的分类优先。

结构化画像的偏好分组独立于 Fact.category：先使用关系中的明确主题（如 favorite_music、likes_music），再根据对象分组；无法判断时放入“其他”。“健康禁忌”仅由规范化后的 allergic_to、allergic、cannot_eat、cant_eat 关系决定，对象中提到过敏或不耐受不足以推断本人有禁忌；例如偏好“过敏知识书籍”归阅读。同一对象命中多个分组时使用固定优先级，例如“有游泳池的酒店”归出行。读取画像不修改事实或历史。

分类和画像分组与敏感检测独立：敏感检测仍检查关系、对象及完整正文，包含会话结论的依据，不随分类修正改变规则。健康禁忌分组不会自动设置敏感标记，例如 cannot_eat/cant_eat → peanuts 在没有其他敏感线索时仍为 False，allergic 则会命中敏感词干。未知拼写 allic 不作为禁忌关系识别。

## 主题与来源

Fact 的 `topic` 是单一主主题，独立于管理分类 category、敏感标记 sensitive 和事实来源 source。固定取值及校验集中在 [topics.py](../../mnemotrace/topics.py)，覆盖生活画像与工作会话，`unknown` 表示未知；不接受自定义主题、中文别名或空字符串。健康主题不证明本人存在疾病或禁忌。

`topic_source` 为 unset、rule、llm 或 user：unset 必须对应 unknown，rule/llm 必须对应明确主题，user 可以明确选择 unknown。写入时按以下顺序确定主题：

1. 显式人工主题优先，包括 unknown/user。
2. 受支持的规范关系或完整主题后缀优先，记为 rule，例如 works_at → work、favorite_music → music。未知关系不拆词猜测，age、name、lives_in 不按 category 强行映射主题。
3. 没有明确关系时接受模型返回的合法已知主题，记为 llm；模型明确返回 unknown 时保持 unknown/unset，不用弱关键词推翻。
4. 无模型、增强关闭、主题缺失或非法时，使用内容规则。单一主题记为 rule，无依据或多个主题冲突时为 unknown/unset。“有游泳池的酒店”允许 travel 优先于 sports；出现第三个主题时仍返回未知。

普通事实的内容规则只检查最终规范关系和对象，主语、拼接正文及整段会话不提供依据；会话结论检查 statement 与 why，排除 session ID。英文使用完整单词和等价分隔符，中文复用常见生活表达并补充明确领域词；物理环境归 environment，技术运行环境归 technology。规则覆盖来自人工合成清单，不代表完整中文语义理解。画像仍沿用自身分组优先级，不受该冲突策略影响。

LLM 主题协议附加在既有属性抽取及结论提取请求中，保留自定义提示词和用户补充指令，不增加独立调用或提高 token 上限；协议和输出会增加 token。只接受精确英文枚举；非法主题不丢弃合法事实，只按数量记录日志并回退。日志在单次抽取调用内聚合：每轮属性提取中每个 episode、每次会话结论提取至多产生一条非法主题 WARNING，不跨 episode 或 session 调用累计；不记录正文、用户标识或非法原值。增强关闭时忽略模型主题，不产生主题告警。模型不能设置主题来源、事实来源、category 或 sensitive。通过 [llm_topics 配置](../reference/configuration.md#python-config)关闭增强后，保留原有提取功能及请求提示词，忽略模型主题，仍执行规则判断。

手动新增未指定主题时使用规则。实际修改三元组或结论正文时，清除旧主题依据并按新内容执行规则；同次明确指定的新主题优先。`reset_topic=True` 立即按当前内容执行规则，不调用模型，结果可能为明确主题/rule，也可能仍为 unknown/unset。重置或仅编辑主题属于元数据操作，不改变事实来源、正文、向量、时间、有效状态和替代链。其他元数据编辑和回传未变内容保留已有主题与来源。

打开、读取及导入快照原样恢复主题，不回填已有 unknown/unset。重复导入或抽取不会覆盖存量事实的主题，人工 unknown 也会保留；新替代事实独立判断，不继承旧事实的人工标签。验收自动主题使用新建数据。

主题字段可通过 Python、HTTP、MCP 和 SDK 读写；控制台暂不提供主题交互，画像分组与检索排序也尚不消费该字段。脱敏会话报告的占位条目省略 topic/topic_source，共享导出继续过滤敏感事实。

`topic` 与 `focus.track` 含义不同：前者描述单条事实的主题，后者是记忆实例（服务端命名空间）的关注配置，使用自由文本词语匹配 Fact.text，提升显著度并影响排序与冷热层策略。两者不自动映射，修改 topic 不会更改 track 或提升显著度；例如 topic=food 不代表正文为“茶”的事实会命中 track=["food"]。

## 时间与知识更新

- Episode 的 `event_time` 表示事件发生时间，`ingested_at` 表示进入系统的时间。
- Fact 的 `valid_at/invalid_at` 记录有效区间，`created_at/expired_at` 记录认知侧时间。
- `supersedes` 连接新事实与被替代事实。更新与自动冲突处理通常采用失效化，保留被替代事实。
- `as_of` 接受 epoch 秒，过滤当前可用记录。它不是任意历史磁盘快照，也无法恢复已经物理删除的数据；具体有效性判定以 [Fact.is_live](../../mnemotrace/types.py) 和对应读取实现为准。

相同槽位中的不同值只是候选冲突。明确更新、补充和歧义冲突需要区分；可选 LLM 检测出的待确认冲突由用户裁决。用户撰写事实受到自动整合的保护，但用户主动编辑或删除仍可改变它。

## 工作记忆与会话收尾

`Memory.remember(scope="working")` 保存带日期的 Episode，并把内容放入 working 层，避免作为持久画像事实提取；直接调用 `remember_working()` 则只添加 working 条目。

working 的“临时”指可见性与清理生命周期。当前 JSONL 快照包含 `working.jsonl`，活跃条目可能随服务重启恢复；它不等于只在 RAM 中存在。`working_set` 是查询时的工作集合，与上述条目不同。

过期、消费或明确清空后，working 条目不再作为活跃状态返回。`close_session` 可整合未完成事件、补摘要、按模型与配置提取结论，再清理指定 session 的 working。原始 Episode 保留。没有配置 LLM 时，不应期待 session outcomes 生成语义结论。

## 保存与提交边界

`Memory.save(path)` 将纯记录保存到 JSONL 集合，并提交 `manifest.json`。集合包括 episodes、facts、entities、relations、working 和 conflicts；manifest 保存计数、嵌入身份、提交标识以及 focus、policy、身份和导入去重等状态。

加载以 **manifest 声明的已提交前缀** 为边界：

- 前缀内部缺失记录或 JSON 格式错误时抛出 `StoreFormatError`。
- manifest 数量之后的撕裂字节作为未提交尾部丢弃；这里指加载时不接纳尾部，不表示自动改写磁盘文件。
- 路径存在且不是目录时拒绝加载；manifest 不存在时，当前实现按空库处理。
- 当前写入与读取 schema 为 2，其他整数版本抛出 `IncompatibleStoreError`；版本缺失或类型非法抛出 `StoreFormatError`。

保存先刷新并替换集合文件，再替换 manifest。文件锁和提交标识提供并发协调，但多文件快照不等于完整的跨文件事务回滚，也不承诺崩溃发生在任意阶段都能自动恢复旧代次。备份应在暂停写入后复制完整数据目录，见[部署说明](../../deploy/README.md#备份与恢复)。

恢复使用独立于新建对象默认值的字段契约。manifest 必须包含 schema、非空 snapshot_id 和 embedder_id、正整数 embedding_dim、backend、软件版本、创建/更新时间、六类集合计数和 state。提交编号缺失时不再使用旧哈希 token。当前 writer 生成的完整存档继续可读；历史存档缺少这些信息时拒绝读取，不自动转换、补造、重写或删除。

六类本地记录要求当前写入字段完整，唯一允许缺省的字段是 Fact.text（按三元组生成）和 Fact.display（空字符串）；未知附加字段不参与对象构造。JSONL 事实另须有 `_store=hot|cold`。时间、编号、归属、来源、替代关系、失效/消费标记均不能由新建默认值补齐。允许显式的合法空值，例如 provenance=[]、invalid_at=null；空值与字段缺失不同。类型严格校验，数值须有限，整数不接受布尔值；epoch 0 和负时间戳原样保留。LanceDB 的类型化 payload 使用同一记录契约。

事实的 topic/topic_source 为必需字段，并校验取值及来源组合。版本 1 的旧库不自动升级，新格式缺失主题字段属于损坏；打开、保存或导入都不会自动清理旧数据。

state 必须包含 focus（track/mute 字符串列表）、policy（四个策略字符串）、identity（字符串映射）、aliases（字符串列表映射）和 import_seen（字符串列表）。显式空列表/映射合法，policy 的空字符串表示使用内置策略；persona_cache、cold_pages_out 和 cold_pages_in 可缺省，提供时仍需合法。它们的字段规则由[恢复校验实现](../../mnemotrace/store/_validation.py)统一维护。

加载在修改目标存储前完成全部记录与状态的校验、转换及对象准备；保存也在替换第一个集合文件前完成待保存数据和已有 manifest 的校验。格式校验失败不改变已有记忆、持久化表或提交编号。服务的缓存版本检查同样验证 manifest，不能通过暖缓存绕过检查。这不增加后端 I/O 失败时的事务回滚保证。

加载后的旧事实分类回填仍保留，退出工作见 [#18](https://github.com/NotZhu/MnemoTrace/issues/18)。

## 结构化迁移导入

`Memory.import_snapshot()` 和 HTTP `/v1/import` 接受版本 2 的结构化导出，要求顶层版本、facts 和 episodes 列表及各记录必要字段完整。事实行须提供原编号、来源类别、topic/topic_source、双时间轴和替代关系；对话行须提供原编号、session、speaker、content 与 event_time，摘要可缺省。时间须为有限 epoch 数值，不从字符串推断单位或用当前时间补齐。导出字段契约与本地存储不同，不能直接将 JSONL 目录当作迁移载荷。

迁移会为目标用户分配新记录编号、重算向量、重连本批与去重命中的替代链；未包含的替代目标置空，provenance 保留来源编号。目标用户的 focus、policy、identity、aliases 和 import_seen 保持不变；导出的 focus、profile 和 graph 等展示信息不覆盖目标设置。默认分享安全导出允许 episodes=[]。迁移导入不提供整库运行状态备份恢复。

导入先整批校验并准备对象和向量，成功后才写入。任何坏记录使整批以 `ValueError` 拒绝，HTTP 对应 400，错误指出记录位置和字段而不包含记忆正文；不再跳过坏行后返回成功。成功结果中的 malformed 固定为 0。普通聊天历史导入仍遵循其独立的宽松解析规则。

## 嵌入模型一致性

- 嵌入维度不符抛出 `DimensionMismatchError`。
- 即使维度相同，模型 id 不同也抛出 `EmbedderMismatchError`。
- 相同维度不代表向量可比较；跳过身份校验后直接检索会得到错误向量空间的近邻。

更换模型时先备份，使用 `Memory.open(path, allow_mismatch=True, embedder=...)`，立即执行 `reembed()`，再 `save()`。该例外用于显式重嵌入，不是忽略损坏的通用选项。服务端对应开关 `MNEMOTRACE_REEMBED_ON_MISMATCH=1` 也可能触发模型调用、下载或重算，完成后应恢复通常的守卫。

运行时 embedder、LLM 和可调 Config 不作为完整 Python 对象序列化。重开时重新提供运行配置，不能假定快照会恢复所有算法选项。

## 隔离与擦除

直接 Memory 实例通过 user_id 分区，调用方仍能访问实例本身；面向不可信客户端的隔离由 HTTP 鉴权和服务命名空间执行。

单事实删除、槽位清理和命名空间清空的范围不同。删除 Fact 不保证抹掉来源 Episode；静音、失效和敏感标记也不等于物理擦除。槽位批量清理仅接受 live、非手写、处于已确认范围内且不破坏替代链的候选，并校对预期数量。完整擦除还需考虑外部导出、备份和客户端副本。

实现入口：[types.py](../../mnemotrace/types.py)、[persist.py](../../mnemotrace/store/persist.py)、[MemoryService](../../mnemotrace/service.py)。
