# MnemoTrace 开发指南

MnemoTrace（忆痕）为 LLM 智能体提供长期记忆，关注记忆正确性、检索质量、使用成本和可复现性。Python 导入包为 `mnemotrace`，Python/npm 发行包为 `mnemotrace-memory`。

本文件维护仓库共用的开发约定；`CLAUDE.md` 引用本文件。按任务读取相关实现、调用方和测试，模块中的局部规则只补充该模块的特殊要求。

## 项目与入口


| 涉及的工作              | 从这里开始                                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 记忆模型与生命周期      | [types.py](mnemotrace/types.py)、[memory.py](mnemotrace/memory.py)、[consolidate/](mnemotrace/consolidate/)                  |
| 检索、排序与上下文      | [retrieve/](mnemotrace/retrieve/)、[config.py](mnemotrace/config.py)                                                         |
| 持久化、命名空间与服务  | [store/](mnemotrace/store/)、[service.py](mnemotrace/service.py)、[spaces.py](mnemotrace/spaces.py)                          |
| HTTP、MCP 与数据导入    | [server/](mnemotrace/server/)、[mcp/](mnemotrace/mcp/)、[connectors/](mnemotrace/connectors/)、[ingest/](mnemotrace/ingest/) |
| 控制台与 TypeScript SDK | [frontend/](frontend/)、[clients/typescript/](clients/typescript/)                                                           |
| 算法评测与结果检查      | [bench.py](eval/bench.py)、[report.py](eval/report.py)、[validate_results.py](eval/validate_results.py)                      |

Python 支持范围、extras 与打包入口以 [pyproject.toml](pyproject.toml) 为准；前端和 SDK 命令见各自的 `package.json`。跨环境验证与合入检查以 [CI](.github/workflows/ci.yml) 为准。需要理解完整机制时，再查 [docs/](docs/) 对应专题；实现、测试与文档不一致时，先明确预期契约并指出差异。

## 设计与实现

- 围绕任务要求的行为控制改动范围，完成必要的调用链调整；独立的清理和重构另行处理。
- 优先扩展职责合适的现有模块。新增抽象应解决当前存在的重复或变化，并说明它封装什么、由谁调用；接口尽量小，复杂度留在实现内部。
- Python 核心仅依赖标准库。LLM、embedder 和存储后端通过接口接入，可选依赖在对应实现中延迟加载，保留离线可验证的原文、显式事实和存储能力；自动事实抽取使用 LLM。
- 参数含义与默认值在定义处维护；跨层传递应保持明确。注释解释非显然的原因、限制和取舍，格式和静态规范交给现有工具。
- 优化前定位瓶颈，再决定是否增加缓存、索引、并发或新依赖，同时考虑失效、资源释放和失败恢复的代价。
- 接口、数据模型、安全边界或跨模块设计存在未决取舍时，先说明问题、备选方案、影响和验证方法。需要持续引用时保留一份设计说明，工具按需选择；普通修复和局部重构直接实施并验证。

## 接口与数据契约

<a id="workflow"></a>当前尚无外部旧版用户，默认同步修改当前接口与调用方。仅在任务明确要求兼容时增加旧格式、别名或迁移逻辑；对外发布或使用范围变化后重新评估。

- 修改公共接口时，核对参数、返回结构、错误语义和默认值，以及实际受影响的 Python、HTTP、MCP、SDK 和前端调用方；保留各接口有意设计的差异。
- 记忆更新保留来源、有效时间和事务时间，替代关系采用失效与历史链路表达。显式擦除严格遵守用户选择的范围，工作记忆的持久化需要明确的生命周期。
- 持久化变更核对写入失败、并发访问及重启恢复；当前格式的必需信息缺失或不合法时，明确报告损坏，避免静默补造数据。
- 区分空结果、调用失败和降级结果；失败应能被调用方识别，重试与回退应保持可观察，避免吞掉错误后返回成功。
- 生成内容修改来源后重新生成。两个展示首页的来源、共享资源及有意差异见 [landing/](landing/) 和 [生成脚本](scripts/build_landing_pages.py)。

## 验证与评测

<a id="principle-ii"></a>模型抽取契约：自然语言事实抽取、自动生命周期判断和会话结论需要配置 LLM。服务端在本次输入写入前拒绝缺少模型的抽取请求；底层 Memory.remember 仅保存原文及路由策略，由 consolidate 检查模型。`python examples/quickstart.py` 与安装后的 `mnemotrace-quickstart` 使用真实模型，不维护离线完整演示。pytest 使用合成事实与固定模型响应，可离线运行；原文保存、手动事实、已有记忆读取和快照恢复不依赖 LLM。

- 为新增行为和缺陷修复补充能区分正确与错误行为的回归；已有测试足以覆盖时直接复用。测试面向可观察结果，避免只复刻实现细节。
- 先运行受影响的检查，再根据影响面扩大验证。接口、存储、隔离和异步生命周期的变化，应覆盖对应集成边界；用户可见的交互变化按需要实际运行验证。
- 同一代码状态下复用已成功的验证，相关输入变化或仍有未解决疑点时再重跑。文案、排版等低影响修改使用对应静态检查。
- 合入前满足适用的 CI 检查；检查失败时区分本次引入的问题和已有问题，保留检查本身并如实报告。


| 验证目标         | 执行入口                                                                                                                |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Python 目标回归  | 根目录：`python -m pytest tests/<file>.py`；完整离线套件：`python -m pytest`                                            |
| 前端             | `frontend/`：`pnpm test`、`pnpm run build`；build 已包含 TypeScript 检查                                                |
| TypeScript SDK   | `clients/typescript/`：`npm run typecheck`、`npm test`；test 会先执行构建                                               |
| 文档链接与导航   | 根目录：`python scripts/check_repository.py`、`git diff --check`；迁移另查路径消费者                                    |
| 展示页生成一致性 | 根目录：`python scripts/build_landing_pages.py --check`                                                                 |
| 核心与发布检查 | 涉及运行、打包或部署契约时，执行 `python scripts/check_core.py`、`python scripts/check_release.py` 及对应 CI 检查 |

<a id="principle-i"></a>算法与公开成绩：

- 使用 `eval/` 的统一框架验证算法变化，选择与影响相关的数据范围；影响全局排序、预算或公开质量结论时扩大到完整集合。算法决策依据输入证据而非数据集标签。
- 同条件比较保持摄入、骨干、answerer 和 judge 设置一致，明确声明偏离。对外报告端到端质量或成本收益时，同时报告准确率、总 token、p50/p95 延迟及同次运行、相同 answerer 与 judge 的 full-context baseline；区分检索与端到端延迟，考虑运行间波动。
- 公开基准结论使用完整集合，切片仅支持其自身范围的结论。原始日志、精确命令、配置、代码 commit 与必要的未提交 diff 应可获取和复现；失败、未完成和探索结果明确标注。
- 用 `python eval/report.py <日志>` 重推成绩；正式引用前运行 `python eval/validate_results.py --expected-rows <完整题数> --require-complete <日志>`，并确认所引用系统与行数。能力描述和公开措辞以可核验结果为依据，目标单独说明。

## 安全与操作边界

- 服务端执行鉴权与命名空间隔离；客户端选择不能代替权限检查。存储和删除目标验证其位于允许的根目录内，路径不直接拼接原始 tenant/user 文本。
- 示例、fixture 和验证使用合成数据。凭据留在环境或本地配置中，输出日志前脱敏；真实个人数据和密钥不得进入提交。发现已泄露凭据时，标记并处理撤销或轮换需求。
- 在任务授权范围内自主完成本地修改和必要验证；只读任务保持只读。真实服务、付费评测、生产数据、推送和发布按已有授权执行，缺少影响决策所需的信息时先澄清。
- 文件删除、环境清理和 Git 历史改写须有明确范围与授权；保留用户已有改动、运行环境和记忆库。
- 分发与许可遵循 [LICENSE](LICENSE) 和 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。

## 协作与交付

- 开始修改前核对工作区已有改动，分别处理本任务内容；发现无关问题时说明影响，避免顺手扩大范围。
- 普通实现问题自行解决；需要维护者决策的产品语义、接口取舍或授权范围问题，给出具体选项与影响，其他不受阻的工作继续推进。
- 分支名用 `type/short-desc` 英文短描述。
- 提交遵循 [Conventional Commits 1.0](https://www.conventionalcommits.org/en/v1.0.0/)，按语义选择类型；常用类型包括 `feat`、`fix`、`docs`、`refactor`、`perf`、`test`、`build`、`ci`、`chore`、`revert`。
- 主题采用 `type(scope): subject`；`scope` 可选，填写时使用简短英文模块或业务领域，`subject` 使用简洁中文。
- 正文可选，与主题空一行，自由说明背景、取舍及必要验证，不要求固定章节。
- 破坏性变更使用 `type(scope)!: subject`，或在 footer 中写明 `BREAKING CHANGE: 具体影响`；`scope` 仍可省略。
- footer 按需关联 issue，例如 `Closes #123`。
- PR 标题采用相同的 Conventional Commits 格式并概括最终变化；正文按 `## 问题`、`## 变化`、`## 验证` 组织并附 CI 运行链接。
- 合并用 rebase 保持 main 线性并保留提交正文，合并后删除功能分支。
- 只纳入本任务应提交的内容；提交说明解释问题和最终变化，措辞平实，不添加 AI 署名。
- 完成时核对最终 diff、受影响的调用方、文件归属和临时产物；交付实际变化、验证命令与结果、跳过项及剩余限制。中断或失败的验证明确说明其覆盖范围。

## 知识与规则维护

- 用法、契约、机制或长期取舍变化时更新对应权威资料；新增长期文档须有独立用途和读者，并接入已有导航。参数、命令和规则各维护一处，其他页面引用来源。
- 问题状态、任务进度和评审过程放在对应 Issue、PR 或当前任务中；当前技术文档解释现行行为及理由。普通修复通过对话、PR 或 CI 交付，调研仅在需要文件交付时落盘。
- 临时脚本、截图、日志、测试数据及工具运行产物写系统临时目录或 `.artifacts/<task>/`，创建前明确存放位置、生命周期和清理方式。需要长期保留的证据应有明确消费者和可获取的保存位置，不按每次修复自动新增报告或归档目录。
- 任务收尾直接检查文件系统中的本次运行产物，包括空目录和被 Git 忽略的内容，不能只依赖 `git status`。只清理能确认由本任务创建且无需交付的内容；归属不明的保留并说明。
- 重复失误先判断应完善接口、工具、测试还是文字说明。新增规则写明适用条件与要解决的问题；流程和工具变化时合并重复内容、删除失效指导。共用规则在本文件维护，局部规则按实际差异增加。
