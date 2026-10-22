# 用户归属与旧接口退役验收

日期：2026-10-22。范围：Windows / Python 3.12.6 本地工程验收，未部署线上服务。
规格：[接口退役](../specs/006-retire-identity-linking/spec.md)。机器记录：
[validation.jsonl](user_scope_retirement_validation.jsonl)。

## 行为与正确性

删除用户编号关联、解析器与 pickle 转换入口，保留原始 user_id 隔离、default 默认值、
姓名/昵称共指和图谱实体别名。快照只在旧映射为空或全部为自身映射时继续加载，真实关联或
非法字段在修改 store 前明确拒绝；不读取、迁移或删除真实旧文件。

新增归属与持久化回归在修改前为 15 failed / 2 passed，修改后目标集合 93 项通过。
完整测试命令：

```powershell
.venv/Scripts/python.exe -m pytest --basetemp=.cleanup-archive/identity-retirement/pytest-full-native -o addopts='' -q --junitxml=.cleanup-archive/identity-retirement/pytest-full.xml
```

结果：1106 passed、13 skipped，174.02 秒。13 项跳过分别涉及 Windows 的文件符号链接权限、
POSIX mode bits、macOS plutil，以及已安装 pypdf 时不适用的缺依赖分支；逐项见机器记录。
LanceDB 可选后端已安装并参与完整测试。沙箱内导入该异步运行时挂起，完整测试改在允许本机运行时
初始化的环境执行；这不是被跳过的后端测试。

## 零配置与发行

以下检查通过：

```powershell
python scripts/check_zero_setup.py
python scripts/check_release.py
python -m build --outdir .cleanup-archive/identity-retirement/dist
.venv/Scripts/python.exe -m ruff check scripts/check_user_scope_equivalence.py tests/test_user_scope.py
git diff --check
```

zero-setup 使用无可选依赖的系统 Python，检查源码和模块 quickstart、离线 harness、既有证据
validator、论文统计与标准库编译。另建干净虚拟环境，以 --no-index --no-deps 安装实际 wheel，
离开仓库路径运行 mnemotrace-quickstart；未安装 fastapi/lancedb，示例仍可运行。
检查 wheel/sdist 均不含退役解析器、转换模块和发行命令，sdist 不含旧转换测试。
新增文件的 Ruff 检查通过；补充全文件风格检查发现 pipeline/persist 中五项既有风格告警，未扩展修改。

初次测试临时目录、网络下载和 ensurepip 受到沙箱限制，相关失败与成功重试均记录在机器日志。
本地只读检查发现默认数据目录一份存储、无实际编号关联；未扫描远端或其他未配置目录。

## 真实数据等价性（探索性，非 QA benchmark）

使用官方 LongMemEval_S 文件顺序的前 20 条，固定时钟与生成 ID，以 hashing + 规则抽取复用
eval 的摄入、抽取选择和 context_for 读取函数。原始数据 revision 为
`2ec2a557f339b6c0369619b1ed5793734cc87533`，文件 SHA-256 见机器记录。
比较基准提交 `bce329704c8a65bf60734ca43bfc2b394aeabcfb` 与当前代码；运行时存在未提交修改，
[完整运行代码差异](user_scope_context_equivalence.runtime.patch.gz)及其哈希已随
[40 条原始 context 与元信息](user_scope_context_equivalence.jsonl)保存，可还原实际执行代码。

复现（--data 指向下载后的官方 longmemeval_s 文件）：

```powershell
.venv/Scripts/python.exe scripts/check_user_scope_equivalence.py --data <longmemeval_s> --baseline-ref bce329704c8a65bf60734ca43bfc2b394aeabcfb --limit 20 --out results/user_scope_context_equivalence.jsonl
```

| 系统 | context 行数 | 平均上下文 token 估算 | 同题全上下文 token 估算 | 摄入至上下文 p50 / p95 |
| --- | ---: | ---: | ---: | ---: |
| 退役前 | 20 | 13653.75 | 124151.60 | 1248.538 / 1521.756 ms |
| 退役后 | 20 | 13653.75 | 124151.60 | 1330.827 / 1531.483 ms |

20 对 context 内容与 token 估算全部相同；行数、唯一 QID 和运行代码差异哈希已校验。
差异以 gzip 保留原始字节，解压后应用到原提交的源码归档，逐一确认 mnemotrace/eval/scripts 下所有 Python 源码与运行版本一致。
token 为字符数除以 4 的粗估，非 tokenizer 计数；计时包含摄入、选定会话整合和上下文构建，
不是纯检索或端到端 QA 延迟。只跑一次前后比较，不能据此认定速度提升。
未使用 answerer/judge，不报告准确率、不作为公开全集质量成绩；完整 benchmark 未触发。

## 交付边界

本地代码、构建和回归验收完成；不代表 Linux、远端存储或线上部署已验证。
升级前备份并检查历史编号映射，含真实关联时停止该存储升级，不用新版本覆盖保存。
功能关闭原因是取消支持，没有实现身份合并或控制台空间合并。
