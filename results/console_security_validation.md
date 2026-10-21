# 控制台脚本权限与接入凭据验收

日期：2026-10-21。工程安全验收，不是算法效果、准确率或线上部署证明。
代码基线：`11faa7066f01c7393a69375ebc4f13cdc5d04bcd`；在本次工作区修改上运行。
分支：`fix/console-script-credentials`。用户已确认将规则变更与安全修复分为两个本地提交，不推送。
下文各轮“不提交”的记录描述当时验收状态；测试仍按上述基线与工作区执行事实记录，不改写为提交后运行。
后续已合并并推送 `d960e27`；该版本首次远程 CI 的失败及修复见 [Windows 所有者验收](#windows-ci-文件所有者验收)。

## 环境与运行结果

Windows，Python 3.12.6（仓库 `.venv`）、独立无可选包的 `--without-pip` 虚拟环境、Node 24.19.0、已有前端依赖。
临时文件统一位于仓库忽略的 `.mnemotrace/`，未使用个人记忆或供应商接口。

| 验证 | 结果 |
| --- | --- |
| 完整 Python 套件 | 最新 1090 通过、13 跳过，见[TOML 编码验收](#codex-toml-字符串编码验收)；此前生成/渲染 1051 通过、报告脱敏 1011 通过、路径修复 965 通过、初轮 928 通过，跳过均为 13 项 |
| 完整前端套件 | 210 通过，0 跳过；包括实际 PowerShell 与 Git Bash 执行 |
| TypeScript / Vite 生产构建 | 通过；产物引用 `/ui/theme-init.js`，无内联主题脚本 |
| 无可选包环境 zero-setup | 通过，含 quickstart、离线 harness、证据与标准库编译 |
| release / landing 同步 / diff 检查 | 通过 |
| 生产浏览器 | 12 项通过，见 [原始检查](console_security_browser.jsonl) |
| 原 Edge 历史凭据 | 三个实际 origin 均清理，见 [存在性记录](console_security_local_cleanup.jsonl) |

完整 Python 跳过项：Windows 无文件 symlink 创建权限（8 项）；POSIX mode bits（3 项）；
已安装 pypdf 导致缺依赖路径不适用（1 项）；macOS 专用 plutil（1 项）。目录 junction、
Windows 原生 DACL、宽权限拒绝和实际 HTTP socket 均有已执行回归。
本机仅有 docker-desktop WSL 发行版，Docker daemon 未运行，没有可用 Linux Python；
POSIX 权限与 Python 3.10 未在本机运行。既有 Ubuntu/Windows、3.10/3.12 CI 矩阵及新增前端安全任务
在后续推送时执行，本轮没有远程 CI 结果。

## 可重复命令

PowerShell，在仓库根目录；`TEMP` 指向已存在的专用测试目录：

```powershell
$env:TEMP = 'E:/MnemoTrace/.mnemotrace/tmp'
$env:TMP = $env:TEMP
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs --basetemp=E:/MnemoTrace/.mnemotrace/test-delivery-security
.venv/Scripts/python.exe -m pytest tests/test_mcp_credentials.py -o addopts= -q
python -m venv --without-pip .mnemotrace/zero-console-security
.mnemotrace/zero-console-security/Scripts/python.exe scripts/check_zero_setup.py
python scripts/check_release.py
python scripts/build_landing_pages.py --check
.venv/Scripts/python.exe -m ruff check --select I,F,E9 mnemotrace/credentials.py mnemotrace/agent_setup.py mnemotrace/agent_doctor.py tests/test_credentials.py tests/test_mcp_credentials.py tests/test_agent_setup.py tests/test_agent_doctor.py
git diff --check
```

在 `frontend/` 执行：

```powershell
node --test tests/*.test.cjs
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build
```

测试目录须为可丢弃的仓库内专用目录。使用外部终端或允许本机 socket/子进程的执行环境；
受限沙箱中的 TestClient 和 Git Bash 不能代表实际平台行为。
普通终端原始输出位于本机 `results/console_security_*.log`（gitignore，不作为公开算法证据提交）。

## 根因与验收覆盖

对基线提交的 setup 源码只调用纯生成函数，使用合成标记测得：配置内含 Key，安装结果重复打印 Key 两次；
当前模板零次出现该标记，并使用文件引用，见 [基线对照](console_security_baseline.jsonl)。
新增主题启动回归在修复前因 HTML 内联脚本失败；其余回归覆盖隐藏输入回显失败/取消、缺凭据、
互斥输入、私有权限、权限失败前零字节写入、原子提交失败、无效/超长文件、重复安装、更新、
dry-run、私有备份、卸载保留凭据、环境身份不回退、生成输出和 doctor 错误/JSON 脱敏。

真实 stdio 回归将 setup 生成的配置交给实际 MCP 子进程，使用私有文件向真实本机 HTTP 服务鉴权，
检查返回同一合成租户，再由 doctor 的实际配置校验函数读取同一引用。其他 doctor 本地生命周期仍受完整套件覆盖。

浏览器使用构建后的页面和仅监听回环地址的临时服务，独立合成数据目录，hashing embedder，无 LLM。
验证实际 CSP 响应、测试内联脚本被阻止、深/浅主题恢复、损坏/受限存储、主题切换、正常登录/退出、
深层路由刷新、PowerShell/Bash 命令不含 Key、实际复制以及有关系数据的图谱 Canvas 无 CSP 违规。
未构建页面 503、缺失资源 404 与实际 HTTP 头由 `test_ui_static.py` 回归验证。

原浏览器清理与合成验收分开：通过已连接的原 Edge，在用户确认的 `localhost:8000`、
`127.0.0.1:8000`、`localhost:5173` 各自来源的临时页面点击定点清理按钮。
页面只用键存在性检查和两次 removeItem，不读取值、不发送内容、不删除主题、偏好、cookie 或全站数据。
其中三处均曾有 mnemotrace.auth，127.0.0.1:8000 还存在 engram.auth；点击后两键均不存在。
临时页面、标签页、合成服务和清理服务均已关闭，不加入产品接口。

## 凭据路径与配置读取验收

同一分支、同一 Windows/Python 3.12.6 环境与未提交工作区。新增规格、计划、任务和一致性检查先于代码修改；
先运行 4 项回归，全部按预期失败：生成片段保留相对路径，隐藏输入、环境变量和 bootstrap 在缺少 tomllib 时
解析多行 args 抛出 SyntaxError。随后统一路径规范化与配置读取边界，不改前端或记忆算法。

`credentials.credential_path` 展开用户目录并转为绝对路径，不使用 resolve；共享 MCP 参数与验证命令出口使用该规则。
`agent_config` 独立提供 setup/doctor 的 Codex 读取。3.11+ 使用 tomllib；3.10 分支先收集完整语句，
只解析所需字符串与字符串数组。未知表达形式跳过可选复用，doctor 明确失败；已识别凭据引用的安全错误仍阻止写入。

[机器记录](credential_config_validation.jsonl)记录实际命令与数量。新增 [36 项路径/配置回归](../tests/test_credential_config.py)
覆盖相对路径、空格与 ~ 的 CLI/直接生成、各客户端片段、安装及切换工作目录、占位符、生成无写入，
以及单行/多行数组、单双引号、引号内 #、行尾注释、Windows 反斜杠、转义字符、尾逗号和相邻配置表。
隐藏输入、环境变量与 bootstrap 均模拟缺少 tomllib，验证相同目标/Key 重复安装不增加文件；
未知格式可重新安装，文件缺失、内容错误、权限校验失败及实际目录链接仍阻止配置更新。权限失败注入用于验证异常传播，
实际 DACL 与宽权限拒绝由既有凭据测试覆盖。

- 完整 Python：**965 通过、13 跳过，140.27 秒**；跳过分类与上方初轮一致。
- 目标 Python：98 通过、2 跳过；之后增加的 2 项安装路径用例和第 2 项真实传输用例已纳入完整套件。
- [真实 HTTP/MCP stdio](../tests/test_mcp_credentials.py)：安装配置与直接生成参数两种入口，均传入相对路径，
  从另一工作目录启动实际 MCP 子进程，读取同一私有文件并返回同一合成租户；doctor 读取同一引用校验通过。
- 无可选包 zero-setup、release、Ruff I/F/E9 与 git diff --check 均通过；独立环境确认无 fastapi、uvicorn、mcp、httpx、numpy、pypdf、pytest、tomli。
- 实际 PowerShell/Git Bash 命令回归 5 通过、0 跳过；Python 使用函数替身，仅验证 shell 参数与执行顺序，
  不代表本轮重新完成真实终端隐藏输入验收。前端、浏览器与原 origin 清理沿用初轮证据，本次没有改前端或重新清理。

重现命令（同样先设置上方 TEMP/TMP）：

```powershell
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs --basetemp=E:/MnemoTrace/.mnemotrace/test-credential-config-full
.mnemotrace/zero-console-security/Scripts/python.exe scripts/check_zero_setup.py
.venv/Scripts/python.exe scripts/check_release.py
node --test frontend/tests/codexSetupCommand.test.cjs
.venv/Scripts/python.exe -m ruff check --select I,F,E9 mnemotrace/agent_config.py mnemotrace/credentials.py mnemotrace/agent_setup.py mnemotrace/agent_doctor.py tests/test_credential_config.py tests/test_agent_setup.py tests/test_agent_doctor.py tests/test_credentials.py tests/test_mcp_credentials.py
git diff --check
```

本轮新增测试最初的 ~ 用例因测试用户目录未隔离失败，修正 fixture 后通过；沙箱拒绝 junction 创建，
允许本机进程的执行环境验证通过。单独重跑目标文件时曾遗漏 TEMP/TMP，36 项因临时目录不可写报错，
不计为代码失败或通过依据；完整套件使用专用目录且通过。普通过程日志保留在本机 `results/credential_config_*.log`。
原生 Python 3.10、Linux/POSIX 权限仍未运行；缺少 tomllib 的模拟不能替代原生平台验收。
本轮不暂存、不提交、不推送，没有远程 CI、部署或新的质量 benchmark。

## 诊断报告结构与脱敏边界验收

同一分支、Windows/Python 3.12.6 与未提交工作区。修改代码前更新既有规格、计划、任务并完成一致性检查。
新增回归首次执行 **40 失败、3 通过**，复现 Key 与字段、枚举重名引起的结构损坏、doctor CLI 的 KeyError 和 setup 误判。
修复后共享 redact 保留字典键，doctor 的 `_redact_report` 复制报告并仅清理路径、地址、详情、展示命令和 data 中的字符串内容。
顶层成功标志、客户端枚举、固定检查名称、状态和汇总保持原值；原始探针不被修改，成功判断及退出逻辑未作绕过。

[46 项新增回归](../tests/test_doctor_redaction.py)使用合成 Key `ok`、`warn`、`fail`、`status`、`summary`、`checks`、`client`、`codex`、
与固定检查名称相同的文本及普通长 Key，覆盖成功、警告、失败；验证 doctor 文本/JSON、Codex/MCP/bootstrap 的结果判断，
以及详情中的模拟错误/凭据、路径、命令和嵌套内容脱敏。确认非文本值与字段名不变，修改返回数据也不会修改原始探针对象。
与固定字段或枚举同名的字符串正常存在，验收按内容字段检查，不使用整份 JSON 的“零字符串出现”判定。

实际结果见[机器记录](doctor_redaction_validation.jsonl)：

- 目标 Python：**104 通过、2 跳过，2.84 秒**；之后增加的 3 项固定检查名称重名用例已纳入完整套件。
- 完整 Python：**1011 通过、13 跳过，135.65 秒**；跳过分类与前两轮一致，包含 8 项文件链接权限、3 项 POSIX mode、1 项已安装 pypdf 的缺依赖分支、1 项 macOS plutil。
- 无可选包 zero-setup、release、Ruff I/F/E9 与 git diff --check 均通过；独立环境确认无 fastapi、uvicorn、mcp、httpx、numpy、pypdf、pytest、tomli。
- 新增报告用例使用成功/警告/失败探针替身，setup 实际写入隔离的临时配置并调用既有配置校验；完整套件的两项真实 HTTP/MCP stdio 传输也通过。

重现命令（先设置上方 TEMP/TMP，在允许本机 socket/原生子进程的环境执行完整测试）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_doctor_redaction.py tests/test_credentials.py tests/test_agent_doctor.py tests/test_agent_setup.py -o addopts= -q -rs
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs --basetemp=E:/MnemoTrace/.mnemotrace/test-doctor-redaction-full
.mnemotrace/zero-console-security/Scripts/python.exe scripts/check_zero_setup.py
.venv/Scripts/python.exe scripts/check_release.py
.venv/Scripts/python.exe -m ruff check --select I,F,E9 mnemotrace/credentials.py mnemotrace/agent_doctor.py mnemotrace/agent_setup.py tests/test_doctor_redaction.py tests/test_credentials.py tests/test_agent_doctor.py tests/test_agent_setup.py
git diff --check
```

普通过程输出保存在本机 `results/doctor_redaction_*.log`。原生 Python 3.10、Linux/POSIX 权限仍未验证，
本轮未重复真实终端隐藏输入、前端构建、浏览器验证或凭据清理；不改变此前记录的范围，也无新的质量 benchmark、远程 CI 或部署。
保留既有工作区修改，不暂存、不提交、不推送。

## 配置生成与文本脱敏边界验收

同一分支、Windows/Python 3.12.6 与未提交工作区。实施前完成既有规格、计划、任务和一致性检查。
根因是四类渲染对已生成的配置、命令与说明执行全文替换；原始 Key 没有进入底层生成结果，
成品却因固定标识或合法参数同名被改坏。新增回归首次 **37 项全部失败**，覆盖固定模块/配置键、工具说明、
地址、路径、状态、安装输出与 namespace 分离约定；没有增加正则例外或禁止合法 Key。

纯生成函数 mcp_args、codex_toml_block、render_markdown 已移除 api_key 参数；CLI 保持原接口。
结果渲染将 namespace 与远程脱敏 Key 分开传递，验证命令仅接收文件引用或本地 namespace。
四类渲染不再对成品全文脱敏；配置/策略块来自当前生成器，功能参数按精确值转义；自由提示在插入前处理，
render_report 的可选 api_key 参数复用既有报告结构脱敏，不修改调用者对象。没有旧配置块解析或兼容别名。

[39 项新增生成/渲染用例](../tests/test_setup_rendering.py)覆盖七种合成 Key、四个渲染入口、本地 namespace、
dry-run/实际安装、直接生成接口和托管策略块。JSON/TOML/shell 参数解析后与接入参数、安装文件比较，
OpenAI Python 示例用 AST 验证占位符，不执行外部 SDK；诊断详情与跳过原因继续脱敏，固定代码与功能参数保留精确值。
与 Key 同字的产品名、参数和路径不当作凭据回显；不使用全文“字符串零出现”判断。

实际结果见[机器记录](setup_rendering_validation.jsonl)：

- 目标 Python：**173 通过、1 跳过，3.18 秒**；唯一跳过是沙箱拒绝创建 junction，完整套件在允许该操作的环境中通过。
- 完整 Python：**1051 通过、13 跳过，80.32 秒**；跳过分类与前轮一致（8 项文件链接权限、3 项 POSIX mode、1 项 pypdf 缺依赖分支、1 项 macOS plutil）。
- [真实 HTTP/MCP stdio](../tests/test_mcp_credentials.py)：3 项通过；新增场景使用合法合成 Key `mnemotrace`，
  从渲染输出解析 JSON 参数，与安装结果一致后在另一工作目录启动真实 MCP，返回同一合成租户，doctor 校验同一凭据引用。
- 无可选包 zero-setup、release、Ruff I/F/E9 及 git diff --check 通过。

重现命令（设置上方 TEMP/TMP，完整套件在允许本机 socket/原生子进程的环境执行）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_setup_rendering.py tests/test_agent_setup.py tests/test_credential_config.py tests/test_doctor_redaction.py tests/test_agent_doctor.py -o addopts= -q -rs
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs --basetemp=E:/MnemoTrace/.mnemotrace/test-setup-rendering-full
.mnemotrace/zero-console-security/Scripts/python.exe scripts/check_zero_setup.py
.venv/Scripts/python.exe scripts/check_release.py
.venv/Scripts/python.exe -m ruff check --select I,F,E9 mnemotrace/agent_setup.py mnemotrace/agent_doctor.py tests/test_agent_setup.py tests/test_credential_config.py tests/test_mcp_credentials.py tests/test_setup_rendering.py
git diff --check
```

初次修复后有 1 项测试错误地要求 bootstrap 两个目标共用同一路径，实际各自的私有文件可读取同一凭据；
修正为分别核对生成参数及身份，最终通过。静态检查首次报告一处 import 排版，修正后通过。
普通过程输出保存在本机 `results/setup_rendering_*.log`。原生 Python 3.10 与 Linux/POSIX 权限仍未验证；
本轮 shell 为参数解析验收，不宣称重新完成真实终端隐藏输入。未改前端、重复浏览器清理或运行质量 benchmark、远程 CI、部署。
既有工作区修改保留，不暂存、不提交、不推送。

## Codex TOML 字符串编码验收

同一分支、Windows/Python 3.12.6 与未提交工作区。实施前更新规格、计划、任务并通过一致性检查。
根因是 JSON 默认编码把非 BMP 字符转成代理项对，TOML 不接受这些转义；部分路径断言又使用 JSON 解析数组，未验证目标格式。
新加回归首次 **29 失败、9 通过**，复现非 BMP 参数、Unicode 用户/显式凭据目录安装及孤立代理项未被安全拒绝。

私有 `_toml_string` 仅编码 TOML 双引号基础字符串，统一覆盖 command 与全部 args：保留合法 Unicode，
转义引号、反斜杠、ASCII 控制字符和 DEL，孤立代理项返回不含输入内容的固定 CredentialError。
没有引入通用 TOML 写入器、依赖或公共接口变更；JSON/shell、路径规范化、权限与脱敏边界不变，也不自动迁移历史配置。

[38 项新增回归](../tests/test_codex_toml.py)覆盖中文、emoji、非 BMP 汉字、空格/引号/反斜杠、全部 ASCII 控制字符及 DEL；
完整成品编码为 UTF-8 字节交给真实 TOML 解析器，再逐项比较参数。验证高/低孤立代理项和直接传入的代理项代码单元对被拒绝，
已有配置和备份不被改写。相关旧断言从 JSON 解析改为真实 TOML 解析，合法路径夹具保留 Unicode 并显式使用 UTF-8。
实际 Unicode 用户目录、显式凭据目录覆盖直接安装、CLI、bootstrap、dry-run、重复复用、doctor、片段无写入与 native/模拟无 tomllib 读取。

实际结果见[机器记录](codex_toml_validation.jsonl)：

- 目标 Python：**165 通过、1 跳过，8.29 秒**；跳过是沙箱拒绝创建 junction，完整套件在允许该操作的环境中通过。
- 完整 Python：**1090 通过、13 跳过，79.07 秒**；分类与前轮一致（8 项文件链接权限、3 项 POSIX mode、1 项 pypdf 缺依赖分支、1 项 macOS plutil）。
- [真实 HTTP/MCP stdio](../tests/test_mcp_credentials.py)：4 项通过；新增场景从实际安装的 UTF-8 Codex TOML 解析含 emoji/非 BMP 汉字与空格的凭据路径，
  在另一工作目录启动 MCP，返回同一合成租户，doctor 校验同一引用。
- 无可选包 zero-setup、release、Ruff I/F/E9 与 git diff --check 均通过。

重现命令（先设置上方 TEMP/TMP，完整测试需允许本机 socket/原生子进程）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_codex_toml.py tests/test_credential_config.py tests/test_setup_rendering.py tests/test_agent_setup.py tests/test_agent_doctor.py -o addopts= -q -rs
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs --basetemp=E:/MnemoTrace/.mnemotrace/test-codex-toml-full
.mnemotrace/zero-console-security/Scripts/python.exe scripts/check_zero_setup.py
.venv/Scripts/python.exe scripts/check_release.py
.venv/Scripts/python.exe -m ruff check --select I,F,E9 mnemotrace/agent_setup.py tests/test_codex_toml.py tests/test_credential_config.py tests/test_setup_rendering.py tests/test_agent_doctor.py tests/test_mcp_credentials.py
git diff --check
```

普通过程输出保存在本机 `results/codex_toml_*.log`。独立解析 oracle 在模拟屏蔽 tomllib 前保留模块，避免模拟分支反而跳过真实解析；
原生 Python 3.10 与 Linux/POSIX 权限仍未运行，已有 3.10 开发依赖 tomli 的原生执行不以本轮模拟代替。
本轮不改前端、不重复浏览器清理、不运行质量 benchmark 或远程 CI/部署；保留已有修改，不暂存、不提交、不推送。

## Windows CI 文件所有者验收

首次推送版本 `d960e27c5e82224d1bf747aa8ca27e08508e9075` 的
[CI](https://github.com/NotZhu/MnemoTrace/actions/runs/39616474523) 中，Ubuntu Python 3.10/3.12、Web 与容器通过；
Windows Python 3.10/3.12 各为 **111 失败、976 通过、9 跳过**，分别耗时 113.07/67.76 秒。
失败归类为同一 owner 检查引发的 84 个 CredentialError、22 个 SystemExit 与 5 个下游退出码断言。
失败位置为 `_windows_security` 的所有者检查，DACL 安装、受保护标志和描述符读取已经成功。

根因是新建路径只施加 DACL，但随后要求 owner 为当前用户或 SYSTEM，隐含假设了创建时的默认 owner。
[Windows 默认所有者来自进程令牌](https://learn.microsoft.com/en-us/windows/win32/secauthz/owner-of-a-new-object)，不保证是当前用户。
现在新建目录和临时文件的 SDDL 显式包含当前用户 owner，并在同一次 SetFileSecurityW 中设置 owner 与受保护 DACL。
已有凭据引用仍只读校验，管理员组 owner 不被加入允许列表；赋权失败在写入前终止，不覆盖目标。

[4 项新增回归](../tests/test_windows_credential_owner.py)首次 **2 失败、2 通过**，修复后全部通过。
隔离模型将默认 owner 设为 Administrators，只模拟文件 owner 状态，SID/安全描述符转换、DACL 安装与校验使用真实 Windows API；
覆盖目录、原子临时文件和备份、重复复用、不安全 owner 只读拒绝与 owner 设置失败时零字节写入/原目标不变。
本机普通令牌不能创建 Administrators 所有的文件（原生探针错误 1307），因此该模型不冒充管理员令牌原生验收。

本机目标凭据测试 **12 通过、2 跳过**（文件链接权限与 POSIX mode）；完整 Python **1094 通过、13 跳过，136.58 秒**，
包含真实 HTTP/MCP stdio 接入。跳过分类与前轮相同；无可选包 zero-setup、release、Ruff I/F/E9 与 diff 检查通过。
zero-setup 首次调用遗漏专用 TEMP/TMP，沙箱临时目录访问失败；设置仓库专用目录后通过，不计为代码缺陷。
完整 Python 和修复版本原生 CI 结果记录于 [机器记录](windows_credential_owner_validation.jsonl)。
此次不改前端、CI 用户或测试跳过条件，不重复浏览器清理，不运行质量 benchmark 或部署。

重现命令（TEMP/TMP 使用上方仓库专用目录）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_windows_credential_owner.py tests/test_credentials.py -o addopts= -q -rs
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs --basetemp=E:/MnemoTrace/.mnemotrace/test-windows-owner-full
.mnemotrace/zero-console-security/Scripts/python.exe scripts/check_zero_setup.py
.venv/Scripts/python.exe scripts/check_release.py
.venv/Scripts/python.exe -m ruff check --select I,F,E9 mnemotrace/credentials.py tests/test_windows_credential_owner.py
git diff --check
```

## 过程限制

- pnpm 的 Corepack 启动器尝试联网下载自身，因网络受限失败；改用已安装的同一 TypeScript/Vite 工具完成检查，无依赖变更。
- 沙箱内 TestClient 被中断、Git Bash DLL 初始化失败；在允许本机 socket/原生子进程的环境中重跑后通过。
- 临时服务启动曾遇自动审批额度失败，恢复审批后成功执行。清理端口初次绑定失败；最终 8000 使用 IPv4 回环，5173 使用 IPv6 回环，无系统网络设置变更。
- 主题脚本初次构建因源地址未匹配 Vite public 资源产生警告；改为标准 public 地址后，最终生产构建无该警告且输出路径正确。
- 浏览器初次任务空间丢失，经用户确认重建。等待隐藏复制按钮的首次检查超时，观察 DOM 后通过悬停实际可见控件完成复制；这些失败尝试未计为通过。
- 私有文件仍是权限保护的明文；旧配置、终端历史和历史备份未自动迁移、清扫或撤销。本机清理不证明其他浏览器配置或远端部署已经处理。
