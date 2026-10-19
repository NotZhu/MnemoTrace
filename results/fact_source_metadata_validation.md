# 事实元数据编辑保留来源验收

日期：2026-10-14。全部验证使用合成记录、规则抽取器、哈希向量及隔离测试目录。
没有读取或修改真实记忆，没有外部模型调用；本记录不是记忆质量 benchmark。

## 原因与修复

自动抽取的事实只切换敏感开关后也出现“你设定”，关闭敏感后仍然显示。
`Memory.update_fact` 在内容未变化的分支中仍执行 `f.source = "user"`；
直接调用后端即可复现，空补丁也会将 extracted 改为 user，因此不是前端缓存或正文变化。

现在删除该分支的来源改写，只更新明确提交的敏感标记/分类，并保留原 source。
实际修改 subject/predicate/object 的原分支仍设置 source=user。
原有双时间历史、正文、展示文本、向量、provenance 和图边保留规则不变。
HTTP、MCP、服务返回值及前端标签继续使用同一 source，相关说明已同步。
未自动回滚过去误标的记录，因为仅凭现有 source=user 无法区分真正的用户断言与误标。

## 验证

回归测试先于修复运行：原有及内容修改测试通过，6 个自动抽取事实的元数据保存用例失败，
均命中 `metadata saves must not change fact ownership`。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_fact_metadata_updates.py -q
```

修复后重新执行最初的完整表单敏感开关复现：

```text
sensitive=True, source=extracted
sensitive=False, source=extracted
original repro: PASS
```

核心测试覆盖 extracted/user 两种来源、元数据单独补丁/完整未变表单、敏感开关往返、
分类修改、空保存，以及主体/关系/内容的实际修改仍标为 user 并保留来源对话。
新增 HTTP 用例 `test_metadata_edit_preserves_extracted_source_across_reload` 验证
PATCH 返回值、落盘后服务重新加载、敏感/分类生效和 provenance 保留；实际修改内容后
重新加载仍为 user。

相关回归 **148 passed**，退出码 0。原始输出见 `fact_source_metadata_pytest.txt`。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_fact_metadata_updates.py tests/test_server.py tests/test_conflict.py tests/test_product_fixes.py tests/test_outcomes.py tests/test_persist_roundtrip.py tests/test_smoke.py -q -ra -o addopts='' -o faulthandler_timeout=30
```

Windows 沙箱内 TestClient 在 asyncio 创建 socketpair 时挂起，本次测试进程已终止。
获准在沙箱外运行相同离线回归后全部通过；存在 2 条 Starlette/httpx/AnyIO 弃用提示。
