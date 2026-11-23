
# MnemoTrace

[简体中文](README.zh-CN.md)

MnemoTrace provides long-term memory for LLM agents. The Python distribution and import package are both named `mnemotrace`. Deployment files for the HTTP API and write worker are in [deploy/](deploy/).

## Local deployment

Start with an empty database. Fill the repository root `.env` using the [database settings](deploy/.env.example), and fill `deploy/.env.models` using the [model settings](deploy/.env.models.example). From the repository root, run:

```sh
docker compose --env-file .env -f deploy/compose.yaml up --build --wait
```

Compose provisions pgvector and database roles, then runs migrations and grants before starting the API and worker. Before running migrations against a separately managed database, install pgvector in the `public` schema.

## Development checks

Run these commands from the repository root.

```sh
uv sync --locked
uv run --locked python scripts/check.py
```

Set `MNEMOTRACE_TEST_POSTGRES_DSN` to point to a dedicated PostgreSQL instance with pgvector for database integration tests. After deployment, run `uv run --locked python scripts/http_smoke.py --help` for the HTTP smoke test options and credential configuration.
