# MnemoTrace 0.1.0 自托管部署

本目录提供两条受支持的单节点部署路径：Docker Compose（推荐）和 systemd。生产默认必须配置 API key，
服务只绑定本机端口，再由 TLS 反向代理或 API 网关对外暴露。

## Docker Compose（推荐）

```bash
cp deploy/.env.example deploy/.env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

把生成值写入 `deploy/.env` 的 `MNEMOTRACE_API_KEYS=tenant:key`，然后：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

Compose 默认：

- 仅监听 `127.0.0.1:8000`。
- 不启用 `MNEMOTRACE_OPEN`/匿名模式。
- 非 root 运行、根文件系统只读、丢弃 Linux capabilities。
- 数据保存到 `mnemotrace_data` 卷的 `/data`。
- `/ready` 未通过时容器不会变为 healthy。

## 密钥轮换

同一租户可同时配置两个 key：

```text
MNEMOTRACE_API_KEYS=tenant-a:key-new,tenant-a:key-old
```

先加入新 key 并重启，切换所有客户端，确认新 key 可用后删除旧 key 再重启。一个 key 映射到两个租户会使
服务 readiness 失败；空租户、空 key 和缺失分隔符也会被拒绝。key 须使用非空、可打印 ASCII，
不能包含中文或控制字符；租户 ID 可用中文。`MNEMOTRACE_SPACES` 的 key 使用同一字符规则。
非法 key 配置令 `/ready` 返回 503，相关鉴权请求返回配置错误；请修正配置后重启，不自动转换 key。

## 反向代理清单

- TLS 终止与证书自动续期。
- 只转发受信 Host，设置请求体上限不高于 `MNEMOTRACE_MAX_REQUEST_BYTES`。
- 按租户或客户端做速率限制，访问日志不要记录 `Authorization`。
- 仅将 `/health` 用作 liveness，将 `/ready` 用作 readiness。
- 管理界面与 API 保持同源；不要把开放模式暴露到公网。

## 用户编号与旧快照边界

记忆按原始 user_id 隔离，不提供编号关联或空间合并。姓名、昵称共指仍在同一用户范围内工作。
旧 pickle 转换模块和命令已退役，不读取、转换或删除已有旧文件。JSONL 集合格式保持不变，
包含真实历史编号关联或非法关联字段的快照会明确拒绝加载，不静默改变数据归属。
升级前停止共享数据目录的全部服务进程并备份，检查 manifest 的 state.resolver_parent 是否存在
非自身映射；发现关联时保留原文件并停止该存储的升级，不使用新版本覆盖保存。
未关联快照正常加载，下一次保存不再写入旧映射。全部进程使用同一版本后再恢复服务。
本次本地验收不代表线上部署已完成，证据见[退役验收](../results/user_scope_retirement_validation.md)。

## 备份与恢复

备份前暂停写流量或停止容器：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml stop
docker run --rm \
  -v mnemotrace_mnemotrace_data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine sh -c 'tar -C /data -czf /backup/mnemotrace-$(date +%Y%m%d-%H%M%S).tgz .'
docker compose --env-file deploy/.env -f deploy/docker-compose.yml start
```

卷名以 `docker volume ls` 实际输出为准。恢复时使用新卷解包，先启动临时实例完成 `/ready`、写入/召回和
导出检查，再切换正式流量。不要只备份单个 JSONL；必须连同 `manifest.json` 和全部集合一起备份。

## 升级与回滚

服务对每个 namespace 只使用“可读前缀 + 摘要”目录；已取消账号名目录与 `.pkl` 候选的运行时回退。
旧目录不会被服务读写或清空。如仍有需要保留的旧数据，先停用共享目录的全部进程并备份，确认
真实归属后显式复制到该 namespace 尚不存在的摘要目录，验证后再处理旧来源；不要仅凭目录名认领数据。
既有摘要目录和 JSONL/manifest 格式不变。详细验证与迁移边界见
[仅摘要目录验收](../results/digest_only_namespace_validation.md)。

已有部署不会被自动接管：请停用旧服务，备份并显式恢复数据到新卷或新目录，再启用
`mnemotrace.service` / 新 Compose 服务。新默认本机数据目录是 `~/.mnemotrace/data`；
容器和 systemd 分别显式使用 `/data`、`/var/lib/mnemotrace`。不会读取旧默认目录、旧环境变量
或旧服务配置来回退。请迁移产品变量为 `MNEMOTRACE_*`，模型供应商自己的变量名保持不变。

1. 备份整个数据卷并验证归档可读。
2. 阅读根目录 `CHANGELOG.md` 的兼容性说明。
3. 构建/拉取明确版本标签，不使用不可追溯的浮动镜像。
4. 启动后检查 `/health`、`/ready`，再做一个合成租户的写入/召回冒烟。
5. 保留旧镜像和当前版本二进制直到观察期结束。

0.1.0 起新租户使用摘要目录。回滚到 0.1.0 以前的二进制时，旧程序可能看不到新目录；因此回滚前必须保留
0.1.0 二进制并先导出/备份，不要尝试手工改目录名。

## systemd 备选

将代码和虚拟环境安装到 `/opt/mnemotrace`，把密钥配置写入 root-only 的 `/etc/mnemotrace/mnemotrace.env`：

```bash
sudo install -d -m 0755 /opt/mnemotrace
sudo install -d -m 0700 /etc/mnemotrace
sudo install -m 0600 deploy/.env.example /etc/mnemotrace/mnemotrace.env
sudo cp deploy/mnemotrace.service /etc/systemd/system/mnemotrace.service
sudo systemctl daemon-reload
sudo systemctl enable --now mnemotrace.service
sudo journalctl -u mnemotrace.service -n 100 --no-pager
```

编辑 `/etc/mnemotrace/mnemotrace.env` 时删除 `MNEMOTRACE_PORT`（systemd 单元固定本机 8000）并替换强密钥。
服务使用 `DynamicUser`、`StateDirectory=mnemotrace`，数据位于 `/var/lib/mnemotrace`。

## 已知边界

本版本支持单节点自托管，不包含多区域 HA、自动分片、企业 SSO/RBAC、计费、监管认证或托管云 SLA。
商业许可和支持条款见 [`../COMMERCIAL-LICENSE.md`](../COMMERCIAL-LICENSE.md)，安全报告见
[`../SECURITY.md`](../SECURITY.md)。
