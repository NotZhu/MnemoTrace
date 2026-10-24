# 为 MnemoTrace 做贡献

[AGENTS.md](AGENTS.md)是项目规则的唯一来源。本页说明开发操作；约束见[零配置](AGENTS.md#principle-ii)、[工作流程](AGENTS.md#workflow)、[验证要求](AGENTS.md#验证与完成)及[证据规则](AGENTS.md#evidence-artifacts)。

## 开发环境

```bash
python -m venv .venv
```

激活虚拟环境（POSIX：`source .venv/bin/activate`；PowerShell：`.venv\Scripts\Activate.ps1`），再安装：

```bash
python -m pip install -e '.[dev,server,mcp]'
```

服务和 MCP extras 仅供对应测试使用，核心仍零硬依赖。前端和 SDK 操作见 [frontend](frontend/README.md) 与 [TypeScript SDK](clients/typescript/README.md)。

## 常用检查

```bash
pytest
python scripts/check_repository.py
python scripts/build_landing_pages.py --check
python scripts/check_zero_setup.py
python scripts/check_release.py
git diff --check
```

先运行受影响的目标测试，提交前按变更范围完成既有门禁。zero-setup 在无可选服务条件下执行示例、合成 harness、公开证据和论文统计，不需要真实模型 API。

## 重大变更

重大任务按 [AGENTS 工作流程](AGENTS.md#workflow)使用 Spec Kit。使用时间戳创建，避免完成规格退出后编号复用：

```bash
bash .specify/scripts/bash/create-new-feature.sh --json --short-name my-feature 'Describe the major change'
```

工具会返回 feature 路径，本地 `.specify/feature.json` 保存当前上下文且被 Git 忽略；可用 `SPECIFY_FEATURE_DIRECTORY` 显式指定。`plan`、`tasks` 解析允许没有研究、模型和契约附属文件。实施前运行 speckit-analyze，通过后再 implement；完成后提炼内容并移除套件。

Spec Kit 不自动更新 AGENTS，也不改 Git 分支；分支命名按任务要求选择。没有活动规格时工具应明确提示，不猜测历史计划。

工作流调度需要先在本机安装所选 Agent 集成；本机安装状态不进入 Git。仓库保留公共脚本与模板的工具安装基线，供升级时识别本地修改。

## 文档与实验

文档入口是 [docs/README.md](docs/README.md)。当前说明更新对应主题；设计取舍和未完成状态分别在[决策](docs/decisions.md)与[台账](docs/issues.md)，不新增每次修改的报告。

临时输出使用 `.artifacts/<task>/`，原始实验保留所需的命令、数据范围、代码条件、失败和限制。有明确消费者后才进入 [results](results/README.md)。普通通过输出留在对话、PR 或 CI。

算法运行、schema 和校验见 [eval](eval/README.md)，公开数字和完整复现见 [RESULTS.md](RESULTS.md)。历史原始日志保持内容不变，当前路径由索引提供。

## 静态网页

HTML 来源为 `landing/index.html.in` 和 `landing/pages.json`。六组共享资源以 `demo/assets` 为源，`docs/assets` 对应内容由生成器同步：

```bash
python scripts/build_landing_pages.py
python scripts/build_landing_pages.py --check
```

提交来源及两份生成输出；不手改生成 HTML 或共享资源副本。页面差异和发布路径保持独立，操作见 [landing](landing/README.md)。

## 提交与部署

PR 描述交代问题、最终行为、验证及限制。重构与新功能分开，移除无消费者的过程材料；临时产物不进入 diff。发布与部署入口见 [deploy](deploy/README.md)。
