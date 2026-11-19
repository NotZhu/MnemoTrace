# MnemoTrace 新后端

`mnemotrace-next` 是为重构准备的独立 Python 项目，临时导入名为 `mnemotrace_next`。

本地与 CI 都用 `.python-version` 选择 Python。uv 会为后端创建虚拟环境，并按 `uv.lock` 安装项目及开发依赖。默认 `dev` 组包含运行推理测试所需的 `inference` extra。先安装 uv，再从仓库根目录运行下面的命令。 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

```sh
cd backend
uv run --locked python scripts/check.py
```

同一条命令在 CI 中依次执行 Ruff lint、Ruff 格式检查、mypy 和 pytest。可以用 `uv run --locked ruff format .` 应用格式化。

运行 `uv run --locked python -m mnemotrace_next` 进行冒烟检查，预期输出 `ok`。

## Python 写入

在服务端用已授权的 `space_id`、绑定认证账号的 `storage`、已装配的 `llm` 和 `embedder`、`clock` 与 `ids` 创建 `Memory`。缺少任一推理端口时，构造函数抛出 `ValueError`。日历日期默认按 UTC 解释，可通过 `timezone` 指定时区。

```python
from mnemotrace_next import Memory, MessageInput

memory = Memory(
    space_id=space_id,
    storage=storage,
    llm=llm,
    embedder=embedder,
    clock=clock,
    ids=ids,
)
session_id = await memory.create_session()
result = await memory.remember(
    (MessageInput(role="user", content="以后导出报表默认用 CSV。"),),
    session_id=session_id,
)
```

`remember` 也接受单条用户文本。`RememberResult.message_ids` 按输入顺序返回已提交原文的标识，`fact_changes` 按审计游标升序返回实际发生的变更。合法零事实或全部暂缓时仍保存原文，事实变更为空。

依赖前一次对话的调用应等待前一次 `remember` 完成。`SessionUnavailable` 表示会话不可用，`RememberInvalidated` 表示空间擦除使本次请求失效，`RememberRetryRequired` 表示并发导致本次尚未写入、可由调用方重新发起。`CommitOutcomeUnknown` 表示写事务进入提交阶段后失去确认，原文和事实可能已经提交。调用方重送会按新写入处理，可能产生新的原文记录。

PostgreSQL 的迁移、角色配置、表结构及真库验证见 [PostgreSQL 存储适配器](docs/postgres.md)。

补全和嵌入的端点配置、向量身份及真实端点验收见 [推理适配器](docs/inference.md)。

发行包随附仓库的[开源许可](LICENSE)和[商业授权条款](COMMERCIAL-LICENSE.md)。
