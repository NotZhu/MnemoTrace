# 工程验收证据

此目录记录工程行为与历史验证范围，不证明记忆准确率。当前契约见[架构](../../docs/architecture/README.md)，操作见[贡献指南](../../CONTRIBUTING.md)。原始 JSON/JSONL、必要 patch 和源码快照保持字节不变。

历史说明链接固定到[历史快照 tag](../README.md#历史快照-tag)，包含当时命令、环境、失败与未执行项；该快照 commit 不替代运行代码 SHA。已记录的旧路径和时间保留原义，未知运行条件不补造。

<a id="release-0.1.0"></a>

## 单节点发布

版本、许可、打包、容器、鉴权及重启恢复。历史本地工程验收不代表线上部署完成，也不是算法质量证明。

| 原始产物 | 用途 |
| --- | --- |
| [commercial_release_0_1_0_validation.jsonl](release-0.1.0/commercial_release_0_1_0_validation.jsonl) | 历史工程运行记录 |

历史说明：[commercial-release-0.1.0.zh-CN](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/docs/commercial-release-0.1.0.zh-CN.md)。

<a id="service-transactions"></a>

## 服务事务与隔离

摘要目录、用户归属、跨实例快照及平台锁回归。source patch 和上下文等价检查属于对应修订，不应推广为所有存储或裸 Memory 的事务保证。

| 原始产物 | 用途 |
| --- | --- |
| [issue033_034_ci_validation.jsonl](service-transactions/issue033_034_ci_validation.jsonl) | 历史工程运行记录 |
| [issue033_034_validation.jsonl](service-transactions/issue033_034_validation.jsonl) | 历史工程运行记录 |
| [user_scope_context_equivalence.jsonl](service-transactions/user_scope_context_equivalence.jsonl) | 历史工程运行记录 |
| [user_scope_context_equivalence.runtime.patch.gz](service-transactions/user_scope_context_equivalence.runtime.patch.gz) | 历史工程运行记录 |
| [user_scope_retirement_validation.jsonl](service-transactions/user_scope_retirement_validation.jsonl) | 历史工程运行记录 |

生成上下文等价证据的[历史工具](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/scripts/check_user_scope_equivalence.py)随专项验证完成退役；该源码与原始补丁用于复核当时行为。

历史说明：

- [digest_only_namespace_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/digest_only_namespace_validation.md)
- [flock_error_handling_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/flock_error_handling_validation.md)
- [forget_concurrency_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/forget_concurrency_validation.md)
- [import_cli_digest_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/import_cli_digest_validation.md)
- [issue033_034_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/issue033_034_validation.md)
- [namespace_ownership_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/namespace_ownership_validation.md)
- [user_scope_retirement_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/user_scope_retirement_validation.md)

<a id="agent-credentials"></a>

## Agent 凭据与配置

私有文件、Windows owner/DACL、相对路径、TOML 编码、诊断和配置渲染边界。保留失败/基线及本地清理记录，清理一处文件不等于撤销历史凭据。

| 原始产物 | 用途 |
| --- | --- |
| [codex_toml_validation.jsonl](agent-credentials/codex_toml_validation.jsonl) | 历史工程运行记录 |
| [console_security_baseline.jsonl](agent-credentials/console_security_baseline.jsonl) | 历史工程运行记录 |
| [console_security_browser.jsonl](agent-credentials/console_security_browser.jsonl) | 历史工程运行记录 |
| [console_security_local_cleanup.jsonl](agent-credentials/console_security_local_cleanup.jsonl) | 历史工程运行记录 |
| [console_security_validation.jsonl](agent-credentials/console_security_validation.jsonl) | 历史工程运行记录 |
| [credential_config_validation.jsonl](agent-credentials/credential_config_validation.jsonl) | 历史工程运行记录 |
| [doctor_redaction_validation.jsonl](agent-credentials/doctor_redaction_validation.jsonl) | 历史工程运行记录 |
| [setup_rendering_validation.jsonl](agent-credentials/setup_rendering_validation.jsonl) | 历史工程运行记录 |
| [windows_credential_owner_validation.jsonl](agent-credentials/windows_credential_owner_validation.jsonl) | 历史工程运行记录 |

历史说明：[console_security_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/console_security_validation.md)。

<a id="console-lifecycle"></a>

## 控制台生命周期与分页

登录周期、旧响应、导出、错误隔离、草稿及一致分页。历史 issue016-037 包保存合成浏览器证据、实现 patch 与源码快照；脚本含当时本机路径，应按原提交及 patch 恢复布局后复现，不能当作当前可移植测试入口。

| 原始产物 | 用途 |
| --- | --- |
| [console_pagination_browser.json](console-lifecycle/console_pagination_browser.json) | 合成浏览器/DOM 检查 |
| [console_pagination_checks.json](console-lifecycle/console_pagination_checks.json) | 合成浏览器/DOM 检查 |
| [console_pagination_consistency_browser.json](console-lifecycle/console_pagination_consistency_browser.json) | 合成浏览器/DOM 检查 |
| [console_pagination_consistency_checks.json](console-lifecycle/console_pagination_consistency_checks.json) | 合成浏览器/DOM 检查 |
| [console_pagination_write_barrier_browser.json](console-lifecycle/console_pagination_write_barrier_browser.json) | 合成浏览器/DOM 检查 |
| [console_pagination_write_barrier_checks.json](console-lifecycle/console_pagination_write_barrier_checks.json) | 合成浏览器/DOM 检查 |
| [issue035_validation.jsonl](console-lifecycle/issue035_validation.jsonl) | 历史工程运行记录 |
| [issue036_browser_checks.mjs](console-lifecycle/issue036_browser_checks.mjs) | 合成浏览器/DOM 检查 |
| [issue036_browser_fixture.mjs](console-lifecycle/issue036_browser_fixture.mjs) | 合成浏览器/DOM 检查 |
| [issue036_validation.jsonl](console-lifecycle/issue036_validation.jsonl) | 历史工程运行记录 |

历史复现包：

- [browser-baseline.mjs](console-lifecycle/issue016-037/browser-baseline.mjs)
- [browser-common.mjs](console-lifecycle/issue016-037/browser-common.mjs)
- [browser-network.mjs](console-lifecycle/issue016-037/browser-network.mjs)
- [browser-verify.mjs](console-lifecycle/issue016-037/browser-verify.mjs)
- [fault-proxy.cjs](console-lifecycle/issue016-037/fault-proxy.cjs)
- [finalize-evidence.py](console-lifecycle/issue016-037/finalize-evidence.py)
- [implementation.patch](console-lifecycle/issue016-037/implementation.patch)
- [metadata.json](console-lifecycle/issue016-037/metadata.json)
- [recheck.jsonl](console-lifecycle/issue016-037/recheck.jsonl)
- [run-command.cjs](console-lifecycle/issue016-037/run-command.cjs)
- [conversation-failure.jpg](console-lifecycle/issue016-037/screenshots/conversation-failure.jpg)
- [conversation-recovered.jpg](console-lifecycle/issue016-037/screenshots/conversation-recovered.jpg)
- [fact-failure-desktop.jpg](console-lifecycle/issue016-037/screenshots/fact-failure-desktop.jpg)
- [fact-failure.jpg](console-lifecycle/issue016-037/screenshots/fact-failure.jpg)
- [fact-recovered-desktop.jpg](console-lifecycle/issue016-037/screenshots/fact-recovered-desktop.jpg)
- [fact-recovered.jpg](console-lifecycle/issue016-037/screenshots/fact-recovered.jpg)
- [settings-failure.jpg](console-lifecycle/issue016-037/screenshots/settings-failure.jpg)
- [settings-recovered.jpg](console-lifecycle/issue016-037/screenshots/settings-recovered.jpg)
- [serve-validation.py](console-lifecycle/issue016-037/serve-validation.py)
- [set-faults.cjs](console-lifecycle/issue016-037/set-faults.cjs)
- [tool-failures.jsonl](console-lifecycle/issue016-037/tool-failures.jsonl)
- [validation.jsonl](console-lifecycle/issue016-037/validation.jsonl)

历史说明：

- [console-validation-index](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/console-validation-index.md)
- [console_pagination_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/console_pagination_validation.md)
- [README](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/issue016-037/README.md)
- [issue035_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/issue035_validation.md)
- [issue036_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/issue036_validation.md)
- [preferences_cache_version_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preferences_cache_version_validation.md)
- [query_isolation_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/query_isolation_validation.md)

<a id="prompts"></a>

## 提示词与消融接线

身份、记录时间、结论提示及偏好标题的合成/定向检查。真实模型示例、离线替身和完整 benchmark 的范围互不替代。

| 原始产物 | 用途 |
| --- | --- |
| [identity_prompt_synthetic_live.jsonl](prompts/identity_prompt_synthetic_live.jsonl) | 历史工程运行记录 |
| [preference_ablation_title_validation.jsonl](prompts/preference_ablation_title_validation.jsonl) | 历史工程运行记录 |
| [record_time_label_synthetic_live.jsonl](prompts/record_time_label_synthetic_live.jsonl) | 历史工程运行记录 |
| [session_outcomes_prompt_synthetic_live.jsonl](prompts/session_outcomes_prompt_synthetic_live.jsonl) | 历史工程运行记录 |

历史说明：

- [identity_prompt_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/identity_prompt_validation.md)
- [preference_ablation_title_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preference_ablation_title_validation.md)
- [record_time_label_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/record_time_label_validation.md)
- [session_outcomes_prompt_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/session_outcomes_prompt_validation.md)

<a id="facility"></a>

## 设施性度量

按机器独立的内容无关计数，区别 L0–L4、服务回归与仪表故障；不发布个人机器数字，不将有用/依赖的未测部分填成零。

| 原始产物 | 用途 |
| --- | --- |
| [facility.jsonl](facility/facility.jsonl) | 历史工程运行记录 |

<a id="maintenance"></a>

## 维护回归

仅保留仍被当前边界消费的结构化检查。普通测试通过文本与界面调整过程由 Git 历史追溯。

历史说明：

- [answer_prompt_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/answer_prompt_validation.md)
- [api_bge_1024_refactor_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/api_bge_1024_refactor_validation.md)
- [api_bge_dimension_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/api_bge_dimension_validation.md)
- [api_key_character_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/api_key_character_validation.md)
- [auth_memory_only_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/auth_memory_only_validation.md)
- [chinese_prompt_context_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/chinese_prompt_context_validation.md)
- [conversations_search_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/conversations_search_validation.md)
- [empty_page_card_layout_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/empty_page_card_layout_validation.md)
- [fact_filters_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/fact_filters_validation.md)
- [fact_metadata_history_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/fact_metadata_history_validation.md)
- [fact_source_metadata_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/fact_source_metadata_validation.md)
- [fact_statement_semantics_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/fact_statement_semantics_validation.md)
- [filter_reset_position_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/filter_reset_position_validation.md)
- [frontend_select_cleanup_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/frontend_select_cleanup_validation.md)
- [graph_branch_layout_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_branch_layout_validation.md)
- [graph_csp_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_csp_validation.md)
- [graph_natural_layout_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_natural_layout_validation.md)
- [graph_quartz_migration_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_quartz_migration_validation.md)
- [graph_readability_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_readability_validation.md)
- [graph_reset_fit_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_reset_fit_validation.md)
- [graph_return_view_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_return_view_validation.md)
- [graph_search_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_search_validation.md)
- [graph_view_style_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_view_style_validation.md)
- [graph_visibility_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/graph_visibility_validation.md)
- [health_edit_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/health_edit_validation.md)
- [health_recheck_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/health_recheck_validation.md)
- [journal_search_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/journal_search_validation.md)
- [login_glass_demo_migration_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/login_glass_demo_migration_validation.md)
- [login_space_guidance_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/login_space_guidance_validation.md)
- [maintenance_cleanup_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/maintenance_cleanup_validation.md)
- [mnemotrace_rename_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/mnemotrace_rename_validation.md)
- [preferences_sha256_fallback_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/preferences_sha256_fallback_validation.md)
- [privacy_export_guidance_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/privacy_export_guidance_validation.md)
- [profile_evidence_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/profile_evidence_validation.md)
- [summary_source_language_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/summary_source_language_validation.md)
- [test_namespace_policy_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/test_namespace_policy_validation.md)
- [theme_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/theme_validation.md)
- [topbar-api-key-reveal-validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/topbar-api-key-reveal-validation.md)
- [ui_chinese_only_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/ui_chinese_only_validation.md)
- [ui_preferences_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/ui_preferences_validation.md)
- [ui_static_boundary_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/ui_static_boundary_validation.md)
- [watch_preflight_validation](https://github.com/NotZhu/MnemoTrace/blob/repository-snapshot-2026-10-23/results/watch_preflight_validation.md)

## 未完成事项

[合成复现与测量限制](open-issues/README.md)仍由[问题台账](../../docs/issues.md)引用。
