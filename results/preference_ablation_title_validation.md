# 偏好消融标题兼容验收（2026-10-18）

仅处理审查第 6 项。偏好证据块改用中文标题后，离线消融仍匹配旧英文标题，
使 `explicit_preference_extraction` 被误判为未命中，现有 pytest 断言失败（23/24）。

`eval/ablate_features.py` 现在接受新中文和旧英文结构化偏好标题，仍要求两项具体偏好证据。
新增回归覆盖两个标题的命中、普通原文块不能冒充偏好块、缺失任一偏好值时拒绝命中。
没有修改生产记忆算法、提示词、系统 ID 或历史 benchmark 日志。

## 复现与验证

修复前单独运行以下测试，稳定失败：`assert 23 == 24`。

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest tests/test_eval_ablation.py::test_offline_feature_ablation_proves_each_enabled_feature_changes_target_evidence -o addopts='' -q --disable-warnings --tb=short
```

修复后的验证命令：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest tests/test_eval_ablation.py tests/test_prompt_language.py -o addopts='' -q --disable-warnings --tb=short --basetemp .pytest_cache/preference-ablation-title-20261018
python -X utf8 eval/ablate_features.py --jsonl results/preference_ablation_title_validation.jsonl
```

12 项相关测试通过，原始输出见 [pytest 输出](preference_ablation_title_pytest.txt)。
两条警告来自关闭第三方插件后未注册的现有 asyncio 配置项。
24 项离线特性消融均满足启用命中、禁用不命中的判据，
逐项结果及 token/延迟记录见 [原始 JSONL](preference_ablation_title_validation.jsonl)。

测试使用合成数据、哈希嵌入器与离线规则，不需要 API key 或外部服务。
验证环境为 Windows / Python 3.12；未运行完整 pytest 或真实模型 benchmark。
