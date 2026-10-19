# 事实元数据编辑保留历史验收

日期：2026-10-13。全部数据为隔离测试中的合成记录，无外部模型或真实记忆读写。
这份记录验证编辑语义，不是记忆质量 benchmark。

## 问题与修复

依次新增 `demo_user / lives_in / 北京` 和 `demo_user / lives_in / 上海`，北京成为历史记录。
原实现对北京执行敏感标记或分类修改后，无条件清空 `invalid_at` / `expired_at`，导致两条都有效。
控制台回传完整但未变化的三元组也会触发此问题。

`Memory.update_fact` 现在先比较三元组的实际值。未变时只处理显式元数据与既有用户署名，
保留时间、替代链、正文、展示文本、向量、provenance 和图边。实际内容修改仍沿用原编辑路径。
不对已被过去操作错误恢复的真实记录作自动推测或回滚。

## 验证

修复前以下命令的四个回归用例全部失败，均命中
`metadata-only edits must not reactivate a historical fact`；修复后四个用例全部通过。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_fact_metadata_updates.py -q
```

新增 HTTP 用例 `test_sensitive_edit_preserves_history_across_reload` 覆盖完整表单 PATCH、
敏感标记开/关、服务重新加载、事实的 live 筛选、图谱有效边、隐私过滤。
核心补充用例覆盖原文/展示/向量/分类保留，以及实际内容编辑仍生效。

相关回归共 202 项全部通过，命令退出码为 0，原始日志见 `fact_metadata_history_pytest.txt`：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_fact_metadata_updates.py tests/test_server.py tests/test_lean.py tests/test_outcomes.py tests/test_persist_roundtrip.py -q -o faulthandler_timeout=30
```

Windows 沙箱内 TestClient 未完成启动；终止本次挂起的测试进程后，在本机隔离临时目录运行同一测试通过。
日志有 Starlette/httpx/AnyIO 依赖弃用提示，没有测试失败。
