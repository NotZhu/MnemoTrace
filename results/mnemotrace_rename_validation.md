# 忆痕完整更名验收记录

日期：2026-10-08。范围：本地源码、配置与运行入口更名；**不是新的 benchmark 测量，也不代表已发布 Python/npm 包**。

## 1. 命名与运行边界

| 对象 | 当前名称 |
| --- | --- |
| 中文产品与界面品牌 | 忆痕 |
| 英文产品 | MnemoTrace |
| Python import / 模块 | `mnemotrace` |
| Python/npm 发行包 | `mnemotrace-memory` |
| 管理界面包 | `mnemotrace-console` |
| 环境变量 | `MNEMOTRACE_*` |
| 命令 / MCP 工具 | `mnemotrace-*` / `mnemotrace_*` |
| 默认记忆根目录 | `~/.mnemotrace/data` |

- `engram/` 已移为 `mnemotrace/`，不提供旧包 shim、旧命令、旧环境变量回退或旧 MCP 工具别名。
- 8 个安装入口为 `mnemotrace-quickstart`、`mnemotrace-mcp`、`mnemotrace-import`、`mnemotrace-migrate-pickle`、`mnemotrace-agent-setup`、`mnemotrace-agent-bootstrap`、`mnemotrace-agent-doctor`、`mnemotrace-watch`。
- 旧 `~/.engram/data` 未删除，未自动迁移，也不再是默认读取目录。显式指定目录的存储安全检查仍保留；这不构成旧产品入口兼容。
- `/v1/*`、`/health`、`/ready`、`/ui/` 等 HTTP 路由不变。服务标识、导出版本键、manifest 产品版本键、OpenAI 响应扩展与默认虚拟模型名改用新名。
- 浏览器状态使用 `mnemotrace.auth` / `mnemotrace.lang`，不迁移旧浏览器登录状态。中文页面标题为“忆痕 · 记忆控制台”，英文为“MnemoTrace · Memory Console”。
- Docker/Compose/systemd/CI、示例、eval imports、SDK、测试及当前说明同步；容器仍挂载 `/data`，新 Compose 项目/卷不自动导入旧卷。

## 2. 私有配置与测试隔离

私有 `.env` 仅改项目变量名并消除重复配置；本文不记录任何密钥、真实租户标识或记忆正文。

- 所有项目变量已为 `MNEMOTRACE_*`；没有重复项目键，也没有残留 `ENGRAM_*` 配置键。
- 有效嵌入设置为 `MNEMOTRACE_EMBEDDER=openai:openai/BAAI/bge-m3`，抽取/回答为 `deepseek`。
- `OPENAI_*`、`DEEPSEEK_*` 供应商配置逐行保持原值；既有服务访问密钥没有替换。
- `.env` 仍被 Git 忽略，未提交或输出私有值。
- `conftest.py` 自动隔离单元测试的项目环境变量与开发者 `.env`；专门测试 dotenv 的用例用 marker 和临时文件显式启用。
- 新测试覆盖配置隔离、重复行/环境优先级、旧环境变量不再作为入口，以及新默认目录不会加载或改写旧目录。
- BGE-M3 初始化为 1024 维；显式 `dim` 优先，其他模型维持既有默认行为。模拟 API 的持久化重启回归通过，仍拒绝真正的维度/模型不匹配。此项不等于已在线验证供应商。

## 3. 实际验证命令与结果

以下命令从本仓库根目录执行；Windows 本机使用 `.venv/Scripts/python.exe`。

| 检查 | 命令 | 实测结果 |
| --- | --- | --- |
| 根目录全量测试，保留真实 `.env` | `./.venv/Scripts/python.exe -m pytest tests/ -q -o addopts=''` | **625 passed, 5 skipped**，51.05s |
| 最终安装/配置/release focused 回归 | `./.venv/Scripts/python.exe -m pytest tests/test_package_metadata.py tests/test_release.py tests/test_configuration_isolation.py tests/test_api_embedder.py tests/test_zero_setup_default.py -q -o addopts=''` | **22 passed**，0.72s |
| 最终文档收尾后的元数据/证据回归 | `./.venv/Scripts/python.exe -m pytest tests/test_package_metadata.py tests/test_release.py tests/test_results_integrity.py -q -o addopts=''` | **29 passed**，0.77s；验收与架构文档本地链接无缺失 |
| 源码离线 demo | `./.venv/Scripts/python.exe examples/quickstart.py` | 通过 |
| 已安装命令离线 demo | `./.venv/Scripts/mnemotrace-quickstart.exe` | 通过 |
| 零设置验证 | `./.venv/Scripts/python.exe scripts/check_zero_setup.py` | exit 0；含 stdlib demo、harness、证据检查、论文统计及编译检查 |
| release gate | `./.venv/Scripts/python.exe scripts/check_release.py` | 全 PASS |
| 刷新新名 editable 元数据 | `./.venv/Scripts/python.exe -m pip install --no-deps -e .` | 成功；版本 0.1.0，无运行依赖升级 |
| 前端标准构建 | `npm --prefix frontend run build` | TypeScript/Vite 构建通过 |
| SDK 按锁安装 | `npm --prefix clients/typescript ci` | 成功，未升级依赖版本 |
| SDK 类型检查 | `npm --prefix clients/typescript run typecheck` | 通过 |
| SDK 标准构建 | `npm --prefix clients/typescript run build` | ESM、CJS 与类型声明通过 |
| SDK 回归 | `npm --prefix clients/typescript test` | **9 passed，0 failed，0 skipped** |
| 工作区空白检查 | `git diff --check` | exit 0；只有本机 LF/CRLF 提示，没有空白错误 |

最终前端 bundle 为 `frontend/dist/assets/index-Dm4R2Zvh.js`（接入页安装命令从 `python3` 统一为 `python` 后重建）；SDK 产物在 `clients/typescript/dist/`。构建目录不作为源码提交。

安装元数据另验证：

- distribution 为 `mnemotrace-memory`，上述 8 个入口均指向 `mnemotrace.*`。
- `importlib.util.find_spec('engram')` 返回 `None`；本机 venv 不再有 `engram-*.exe`。
- 项目 URL 仍是原始真实上游来源，没有捏造新的 GitHub 地址。

## 4. 实际 HTTP 与页面验收

临时服务仅使用 hashing、空 LLM/answerer/vision 设置、合成测试访问密钥、临时数据目录；没有使用真实记忆库，也没有调用在线模型。

启动模块：`python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8000 --workers 1`。

- `/health`：HTTP 200，`service=mnemotrace`，`auth_mode=api_keys`，`anonymous_allowed=false`，embedder 为 HashingEmbedder，LLM/answerer 均未配置。
- `/ready`、`/ui/`：HTTP 200。
- 合成 Bearer 鉴权访问 `/v1/stats`：HTTP 200；episodes、facts、entities、relations 等计数均为零。
- 浏览器 DOM 与截图确认中文品牌“忆痕”、登录页和中文页面标题；新随机 key 使用 `mnemotrace-` 前缀。
- **GUI 登录验收通过（复核）**：以合成 key 在登录页提交后进入总览 dashboard，侧边栏品牌“忆痕”、顶栏“服务在线”、key 脱敏显示，合成命名空间各计数为 0。“接入与策略”页安装命令块显示为 `python -m pip install -e '.[mcp]'`。备注：自动化环境的 Playwright locator click 在该按钮上反复超时，改用截图坐标点击后成功——这是自动化工具路径问题，不是应用缺陷。
- 验收结束后临时服务已停止，socket 检查确认 **127.0.0.1:8000 已释放**；不留后台服务。

## 5. 历史证据、许可证与用户数据保护

更名前以当前工作区字节建立保护基线，而不是用 HEAD 覆盖既有修改。

- 60 个受保护文件的 SHA-256 最终检查：**changed=[]**。
- 保护项包含 49 个原始 results JSONL、`LICENSE`、`COMMERCIAL-LICENSE.md` 及旧记忆目录文件。
- 原本已 dirty 的日志仍按其更名前字节保留；没有恢复为 Git 旧版本。
- 历史 benchmark 系统 ID（`engram`、`engram_lean`、`engram_full` 及消融 ID）、原日志路径、数值、真实上游 URL、作者及版权记录保留。历史 transcript 分析保留旧工具名解析，仅用于历史计数，不是当前 MCP 注册别名。
- 当前 runtime 源码没有旧产品名残留；测试中的旧名称负面断言、历史系统 ID 和真实来源链接属于明确例外。
- 未删除旧记忆，未自动重嵌入，未改用户的外部 agent settings/watch/hook 任务。
- 无 Git add/commit/push，origin 保持 `https://github.com/ly-wang19/engram`；仓库已有未提交修改保留。

## 6. 发布与未验证范围

- 新名发行包未发布到 PyPI/npm。当前安装说明使用本检出 `python -m pip install -e '.[extra]'`，SDK 从 `clients/typescript` 本地构建/安装；不从旧上游 URL 安装新名分发。
- 未运行 Docker build，未验证真实 watcher/systemd 调度。
- 未调用真实硅基流动或 DeepSeek API；密钥有效性、余额、权限与在线响应不在此次离线验收结论内。
- 没有重新跑官方 benchmark；既有评测数值不是更名后的新测量。
- 论文 PDF/tar.gz 保留为历史产物，未重建。

架构范围见 [`docs/architecture-optimization-map.zh-CN.md`](../docs/architecture-optimization-map.zh-CN.md) 与 [`docs/mnemotrace-full-architecture-report.zh-CN.md`](../docs/mnemotrace-full-architecture-report.zh-CN.md)。
