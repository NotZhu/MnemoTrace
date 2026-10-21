# 为 MnemoTrace 做贡献

项目使命和贡献规则统一维护在 [AGENTS.md](AGENTS.md)，适用于人类贡献者与智能体。本页提供操作导航，不维护规则副本。

| 要做什么 | 入口 |
| --- | --- |
| 理解项目方向与设计约束 | [项目章程](AGENTS.md) |
| 配置离线开发与验证零配置运行 | [零配置不变量](AGENTS.md#principle-ii)、[验证与完成](AGENTS.md#验证与完成) |
| 实现功能、修复问题或准备推送 | [工作流程与完成条件](AGENTS.md#workflow) |
| 运行评测、校验日志或引用数字 | [评测规则](AGENTS.md#principle-i)、[结果与复现命令](RESULTS.md) |
| 修改架构文档 | [文档更新](AGENTS.md#documentation) |
| 更新对外文案、提交信息或处理隐私 | [公开内容与贡献规则](AGENTS.md#principle-vii) |
| 构建落地页 | [同步公开入口](AGENTS.md#principle-i)、[生成器](scripts/build_landing_pages.py) |
| 部署或升级服务 | [部署说明](deploy/README.md) |

落地页操作入口：共享内容位于 `landing/index.html.in`，页面差异位于 `landing/pages.json`。生成命令为 `python scripts/build_landing_pages.py`，同步检查为 `python scripts/build_landing_pages.py --check`；发布与证据要求见上表。
