# Changelog

All notable changes to MnemoTrace are documented here. The project uses semantic versioning for distributed
artifacts; storage schema compatibility is documented separately when it changes.

## [Unreleased] - 2026-10-25

### Added

- Added a Compose stack for PostgreSQL 18, the API and an independent worker, with explicit
  role provisioning and migration. Added logical backup and restore verification for schema 6.
- Added explicit space management, durable operation lookup and retry, and the `mnemotrace` CLI for
  operation status, retrieval, and fact deletion.

### Changed

- Removed independent working-memory storage and task-state APIs. Source summaries now use
  `digest` / `digests` throughout Python, HTTP, MCP, SDK, configuration and storage. Recall no
  longer accepts a session ID. This format requires a new database; old schemas and checkpoints
  are rejected rather than converted. Agent task tools or files own temporary execution state.
- Switched the HTTP service, MCP tools, SDK clients, CLI, and console to Postgres-backed memory.
  HTTP and remote MCP verify issued Bearer keys per request, and recall authorizes every requested space.
  Python integrations use `Memory(database, context)`. The offline entry is now `MemoryEngine()`.
  Deployment requires an initialized Postgres
  database and a runtime role connection.
- Buffered ingestion acknowledges durable input and returns operation IDs. Immediate ingestion waits
  for core processing. Auxiliary tasks have their own operation IDs.
- Explicit record erasure accepts facts. Space clearing, space deletion, and account
  deletion use their own operations.
- Rebuilt documentation around quickstart, usage, integration, API references, architecture,
  deployment, and evaluation; consolidated overlapping architecture and frontend development pages.
  AGENTS.md now defines shared engineering guidance with scoped validation and document lifecycles.
- Audited the historical issue ledger and moved 24 remaining items to GitHub Issues;
  removed docs/issues.md and updated its documentation and checker references.
- Result auditing requires explicit log paths or glob patterns instead of an implicit
  archive directory.
- Retained historical material in Git history;
  current documentation no longer carries historical benchmark scores as current claims.
- Unified frontend test discovery in CI, enabled unused TypeScript checks, and narrowed Docker build
  context to exclude local runtime data and scratch files. Pytest cache now uses .artifacts/pytest/.
- Product services use a newly initialized database. Existing file-backed stores are left in place.
- Memory ownership uses the exact supplied `user_id`; names, nicknames, and entity aliases still resolve
  within that scope. Snapshots with actual historical user-ID links fail before any store mutation;
  empty or self-only mappings remain loadable without rewriting records.

### Removed

- Removed the file-backed product store and its LanceDB and Qdrant extras. The in-memory
  algorithm work set remains available for tests and evaluation.
- Retired RESULTS.md, results/, paper/, and Spec Kit scaffolding from the maintained tree;
  engineering checks no longer require their historical files or workflow.
- Completed specification suites, redundant reports, obsolete diagnostics, and stale integration
  inventory. Local scratch files use .artifacts/; the downloaded LOCOMO dataset is no longer vendored.
- The user-ID linking interface and resolver.
- The `close_session` product route, tool, and SDK method. Use session-scoped `flush_processing` to
  wait for core work and the dedicated operation to clear working memory.
- The legacy pickle converter, its console command, and migration guidance. Existing pickle files are
  left untouched and are no longer supported as conversion sources.

## [0.1.0] - 2026-07-14

### Added

- A bounded self-hosted commercial distribution with Python package, HTTP service, MCP surface,
  TypeScript SDK, management console, Docker Compose deployment, and systemd alternative.
- Separate liveness (`/health`) and readiness (`/ready`) semantics.
- Zero-dependency release gate covering versions, licenses, documentation, deployment assets, and raw
  benchmark evidence links.
- Chinese-first deployment, backup, restore, upgrade, rollback, key rotation, security, and release docs.

### Security

- Replaced character-stripped namespace paths with deterministic digest-backed directories.
- Prevented dot-directory traversal, absolute-path escape, normalization collisions, and cross-tenant
  deletion targets while retaining valid legacy directory/pickle compatibility.
- Added strict static API-key parsing, one-tenant/multiple-key rotation, ambiguous-key rejection,
  constant-time key comparison, request-size limits, field bounds, and browser/API security headers.
- Production container runs as non-root, drops Linux capabilities, uses a read-only root filesystem,
  persists only `/data`, and does not enable open mode by default.

### Changed

- Unified the Python package, service, TypeScript SDK, console, and persistence manifest version at 0.1.0.
- Package development status moved from Alpha to Beta with an explicit single-node support boundary.
- Standard self-hosting examples now use configured API keys; open mode remains development-only.

### Compatibility

- Existing safe namespace directories and trusted legacy pickle snapshots remain readable.
- New namespaces use digest-backed directory names. A rollback to pre-0.1.0 binaries may not discover
  those new directories, so back up data and retain the 0.1.0 binary before rollback.
- Memory extraction, retrieval, ranking, context assembly, and published benchmark numbers are unchanged
  by this release.

### Known Limits

- Single-node only; no multi-region HA, automatic sharding, online distributed migration, billing,
  enterprise SSO/RBAC, compliance certification, or hosted-cloud SLA is claimed.
- TLS, WAF, internet-facing rate limits, and centralized secret management belong at the deployment edge.
