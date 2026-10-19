# API 嵌入默认 BGE-M3 / 1024 重构验收

日期：2026-10-18。范围：`LiteLLMEmbedder` 默认模型、固定维度约束、工厂入口及持久化加载。
依据所有者要求，API 默认模型收口到 `BAAI/bge-m3` / 1024，去掉未识别模型一律猜 1536 的逻辑。
这是离线正确性验证，不是模型 benchmark；没有新的准确率、token 或延迟主张。

## 改动

- `LiteLLMEmbedder()` 默认使用 `openai/BAAI/bge-m3`。`openai/` 是 LiteLLM 的 OpenAI 兼容接口
  路由；完整服务配置 `openai:openai/BAAI/bge-m3` 的行为保持相同。工厂的 `openai:` 空模型入口
  现在使用同一 BGE-M3 默认。
- BGE-M3 在首次请求之前固定为 1024 维。错误的 `dim` 配置在构造阶段拒绝，返回向量长度不符
  时抛出异常；检查批量响应中的每一条向量，不再随首次响应修改维度。
- 其他 API 模型仍可由 SDK 显式传入实际 `dim`，但不再猜测维度。`dim` 必须是正整数，拒绝布尔值、
  浮点数、字符串及非正数。仅指定其他 API 模型名称、未提供维度的调用现在会明确报错。
- 服务的全局零配置默认继续为 hashing。核心无新增硬依赖；LiteLLM 继续延迟导入。
- JSONL/manifest、嵌入身份和维度守卫保持原样。测试确认同一 BGE 标识写入后可直接重启加载，
  此时零 API 调用、零重嵌入；错误模型身份和维度仍会拒绝加载。

## 回归与结果

先在现有实现上运行新增/更新的 API 回归：

```text
python -m pytest tests/test_api_embedder.py -o addopts='' -q --tb=short
9 failed, 7 passed
```

其中 `openai:` 服务在保存后重启的路径实际报出：

```text
DimensionMismatchError: store embedding_dim=1024 does not match current embedder dim=1536
```

其他失败覆盖默认模型仍是旧模型、未知模型无维度不报错、BGE 配错维度被接受、响应长度变化
会静默修改维度。修复后增加了非法维度输入回归，并运行以下关联测试。

Windows 沙箱的系统临时目录存在权限限制，因此在仓库根目录配置可写临时目录，测试禁用
第三方插件自动加载，避免开发者配置污染。测试数据均为合成内容。

```powershell
$apiTemp = Join-Path (Get-Location).Path '.pytest_cache/api-bge-refactor-runtime'
New-Item -ItemType Directory -Force -Path $apiTemp | Out-Null
$env:TEMP = $apiTemp
$env:TMP = $apiTemp
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest tests/test_api_embedder.py tests/test_persist_safety.py tests/test_reembed.py tests/test_service_paths.py tests/test_configuration_isolation.py tests/test_zero_setup_default.py -o addopts='' -q --tb=short --basetemp .pytest_cache/api-bge-refactor-final
python scripts/check_zero_setup.py
```

- **58 passed, 2 warnings**。两条 warning 是关闭插件后，已有 asyncio 配置项没有被插件注册。
  范围覆盖 API 嵌入、持久化安全、重嵌入、服务路径、配置隔离与零配置默认。
- `check_zero_setup.py` 完整通过，包含源码与模块 quickstart、离线 harness、持久化冒烟、历史
  原始证据日志校验和标准库编译。校验历史日志不代表重跑了历史模型 benchmark。
- 输出见 [pytest 日志](api_bge_1024_refactor_pytest.txt) 与
  [zero-setup 日志](api_bge_1024_refactor_zero_setup.txt)。日志统一为 UTF-8，并把个人用户目录
  前缀替换为 `<USER_HOME>`；没有记录供应商凭据。

## 使用与兼容边界

服务配置 `MNEMOTRACE_EMBEDDER=openai:` 或 `openai:openai/BAAI/bge-m3`，并向服务进程提供
兼容接口的 `OPENAI_BASE_URL` 与 `OPENAI_API_KEY`。部署说明见 `API.md` 的 API 嵌入小节。
本次没有读取或修改开发者 `.env`、已有记忆或真实供应商配置，也没有自动切换运行中的服务。

其他 API 模型需要在 SDK 层提供维度；没有新增服务端维度环境变量。例如原先依赖隐式 1536 的
调用，现在需明确提供实际维度。显式模型名称继续原样传递，不通过改名或维度相同推断为同一模型。
维持相同模型标识的已有 BGE-M3 / 1024 库可直接重启；其他模型或不兼容的存储需要备份后进行
显式迁移，不修改 manifest 来绕过守卫。

没有在线供应商实测，没有运行完整 pytest、浏览器或容器验收。前次完整测试已遇到当前沙箱的
回环 socket 限制；本次采用覆盖改动链路的定向回归与完整 zero-setup，没有把这些结果当成
完整 CI 或真实模型质量的证明。
