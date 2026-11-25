#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ! ${MNEMOTRACE_DB_PASSWORD:-} =~ ^[0-9a-fA-F]+$ ]]; then
    printf '%s\n' 'MNEMOTRACE_DB_PASSWORD 须为非空的十六进制密码' >&2
    exit 1
fi

if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
    # 官方入口在首次初始化时要求管理员口令，初始化 SQL 会清除该口令。
    POSTGRES_PASSWORD=$(od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]')
    export POSTGRES_PASSWORD
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
