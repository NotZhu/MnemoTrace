# Changelog

All notable changes to MnemoTrace are documented here. The project uses semantic versioning for distributed
artifacts; storage schema compatibility is documented separately when it changes.

## [Unreleased] - 2026-10-24

### Changed

- Refactored documentation into guides, API references, architecture, decisions, and an issue ledger;
  consolidated project rules in AGENTS.md and made document updates depend on the change's scope.
- Classified benchmark, experiment, and engineering evidence under results/; preserved raw artifacts
  and linked historical explanations through the repository-snapshot-2026-10-23 tag.
- Unified frontend test discovery in CI, enabled unused TypeScript checks, and narrowed Docker build
  context to exclude local runtime data and scratch files. Pytest cache now uses .artifacts/pytest/.
- HTTP and local MCP default to `~/.mnemotrace/data`; containers keep `/data` and systemd uses
  `/var/lib/mnemotrace`. Old data directories are neither discovered nor migrated automatically.
- MemoryService now reads, writes, and erases only the namespace's digest directory. Runtime fallback
  to account-named directories or `.pkl` candidates has been removed. Existing digest stores keep the
  same names and format; old paths remain untouched and require explicit, ownership-verified migration.
- Memory ownership uses the exact supplied `user_id`; names, nicknames, and entity aliases still resolve
  within that scope. Snapshots with actual historical user-ID links fail before any store mutation;
  empty or self-only mappings remain loadable without rewriting records.

### Removed

- Completed specification suites, redundant reports, obsolete diagnostics, and stale integration
  inventory. Local scratch files use .artifacts/; the downloaded LOCOMO dataset is no longer vendored.
- The user-ID linking interface and resolver. API-key authentication and space isolation are unchanged.
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
