# MnemoTrace

[简体中文](README.zh-CN.md) · [Quickstart](#quickstart) · [Docs (Chinese)](docs/README.md) · [How it works](#how-it-works) · [Evaluation](#evaluation)

MnemoTrace is an open-source, self-hosted long-term memory engine for LLM agents. Agents can carry project conventions and user preferences into new sessions. When those details change, agents can look up what applies now, what applied at an earlier point in time, and which messages support each fact.

Applications submit conversation messages with `remember`, and a background worker extracts structured facts. Before generating a response, the application calls `recall` for evidence with validity intervals and references to the original messages, then adds the evidence it needs to the model context. The service consists of an HTTP API, a write worker, and PostgreSQL with pgvector.

## Quickstart

Install Docker Compose 2.30.0 or later and `uv`. Use an empty database for the first deployment. Run these commands from the repository root.

1. Copy the [database template](deploy/.env.example) to `.env` at the repository root and generate a distinct hexadecimal password for each of the four database roles. Copy the [model template](deploy/.env.models.example) to `deploy/.env.models` and configure the completion and embedding models.
2. Start the service and issue an API key for the local demo account.

   ```sh
   docker compose --env-file .env -f deploy/compose.yaml up --build --wait
   docker compose --env-file .env -f deploy/compose.yaml run --rm admin issue --owner-id local-demo
   ```

3. Set the `MNEMOTRACE_SMOKE_TOKEN` environment variable to the returned `token` and run the smoke test.

   ```sh
   uv run --locked python scripts/http_smoke.py
   ```

   The script writes synthetic messages, verifies that recalled evidence traces back to the original messages, and removes the test space it created. On success, it prints `HTTP 闭环通过，验收空间已删除。` (HTTP smoke test passed and the test space was removed).

Once the smoke test passes, call the service directly using the [HTTP API v1 guide (Chinese)](docs/reference/http-api.md) or use the [TypeScript SDK (Chinese)](clients/typescript/README.md) in Node.js. See the [self-hosting guide (Chinese)](deploy/README.md) for external PostgreSQL, key rotation, and troubleshooting.

## How it works

### Durable writes and atomic commits

The API persists write requests as jobs in PostgreSQL. Retrying the same batch in the same session with the original idempotency key returns the original job. Writes run in submission order within a session, and different sessions can run concurrently. The worker extracts facts and uses the model to compare candidates outside the space-level write lock, then rechecks the candidates under the lock. It commits the original messages, fact changes, and successful job status in a single transaction. [Write implementation](mnemotrace/application/remember.py)

### Fact adjudication and bitemporal history

MnemoTrace selects prior facts to compare using their subjects, predicates, overlapping validity intervals, and lexical and vector search. The model classifies each pair as duplicate, compatible, conflicting, or uncertain. The backend uses the validity intervals and evidence to decide whether to add support to an existing fact, close its validity interval, or withhold a candidate. [Adjudication](mnemotrace/application/adjudication.py) · [Fact lifecycle](mnemotrace/application/fact_lifecycle.py)

MnemoTrace records **when a fact is valid** and **when the system received it** separately. On May 1, a user says, “Starting today, I’m taking the subway to work on weekdays.” In another session on June 3, they say, “I switched to biking to work on weekdays starting June 1.” The service accepts the update on June 3, and the biking fact is effective from June 1. Once the write commits, `as_of` can look up which commute was valid at a given time.

| Query time | Recalled fact | Source |
| --- | --- | --- |
| May 31 | Takes the subway to work on weekdays | May 1 message |
| June 1, when the change takes effect | Bikes to work on weekdays | June 3 message |

### Hybrid retrieval and context budgets

`recall` filters facts by space and validity interval. Reciprocal Rank Fusion (RRF) combines lexical, vector, and optional multi-hop graph results. The service can then rerank the combined results. `top_k` limits the number of evidence items, and `max_evidence_chars` caps the total character count of their text. The response includes only whole items within that budget. If embedding or reranking times out, the service falls back to available results and reports the reason in `degraded_reason`. [Retrieval implementation](mnemotrace/application/recall.py)

### Evidence provenance and session erasure

Recalled evidence includes `source_message_ids` that point to the original messages. The audit log shows validity intervals before and after a change and identifies the facts and evidence that caused an interval to close. Erasing a session removes evidence groups that depend on its messages. The service removes extracted facts that lose all complete evidence groups and can retain facts backed by independent evidence. [Audit schema](mnemotrace/server/v1/audit_schemas.py) · [Erasure tests](tests/postgres/test_erasure.py)

See the [memory engine architecture (Chinese)](docs/architecture/memory.md) for the data model, runtime flows, and consistency guarantees.

## Evaluation

The evaluation suite covers LongMemEval-S, LoCoMo, and PersonaMem v2. It compares MnemoTrace with full-context and BM25 baselines in the same run. See the [evaluation guide](eval/README.md) for dataset versions, reproduction commands, and publication checks.

## Development checks

Run these commands from the repository root.

```sh
uv sync --locked
uv run --locked python scripts/check.py
```

Set `MNEMOTRACE_TEST_POSTGRES_DSN` to a dedicated PostgreSQL instance with pgvector to run database integration tests.

## License

[AGPL-3.0-only](LICENSE)
