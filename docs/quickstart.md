# 快速开始

按[部署说明](../deploy/README.md)启动服务、签发 key 并创建演示空间。记下账号 ID 和空间 ID。控制台可在同一个空间写入原文、录入人工事实和检索。自动抽取需要配置模型。

## Python 直连

在可以访问 Postgres 的受信任环境安装 Python 包。Compose 的 Postgres 仅开放给内部网络，宿主机直连需要使用自行管理的 Postgres 连接。

```sh
python -m pip install "mnemotrace-memory[postgres,llm,fastembed]"
```

源码安装使用 `python -m pip install ".[postgres,llm,fastembed]"`。设置 `MNEMOTRACE_DATABASE_URL` 为运行角色连接串，并将 `MNEMOTRACE_USER_ID` 和 `MNEMOTRACE_SPACE_ID` 设为已有演示账号和空间。配置 `MNEMOTRACE_LLM` 与模型提供方凭据。Quickstart 默认使用 FastEmbed，首次运行需要下载模型。连接已有服务空间时，使用与服务一致的 `MNEMOTRACE_EMBEDDER` 设置。参数见[配置参考](reference/configuration.md#postgres-数据库基础)。

```sh
mnemotrace-quickstart
```

源码入口为 `python examples/quickstart.py`。示例将合成会话提交到指定空间，输出操作 ID，等待抽取归并，再运行检索和历史查询。持久记录会保留在该空间，重跑使用相同的消息标识。

## 原文与人工事实

用运行角色连接，显式授权后可直接保存原文和人工事实。下面的操作无需配置 LLM。

```python
import os

from mnemotrace import Memory
from mnemotrace.store.postgres import Database, DatabaseConfig, Spaces

with Database(DatabaseConfig.from_env()) as database:
    scope = Spaces(database).authorize(
        os.environ["MNEMOTRACE_USER_ID"], os.environ["MNEMOTRACE_SPACE_ID"],
    )
    memory = Memory(database, scope)
    memory.remember_raw("Synthetic project note", "demo")
    memory.add_fact("user", "lives_in", "Paris")
    print(memory.search("Where do I live?").answer())
```

[HTTP API](reference/http-api.md)提供会话写入、任务查询和检索入口。[使用指南](guides/usage.md)说明空间、会话和删除的操作范围。
