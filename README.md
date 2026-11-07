# MnemoTrace · 忆痕

[![CI](https://github.com/NotZhu/MnemoTrace/actions/workflows/ci.yml/badge.svg)](https://github.com/NotZhu/MnemoTrace/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue.svg)](LICENSE)

Long-term memory for LLM agents, with a Python library, HTTP service, MCP tools, TypeScript SDK, and a management console.

MnemoTrace stores conversation episodes, extracts facts with time and provenance, and retrieves relevant context across sessions. Memory updates can retain superseded facts for historical queries. Natural-language fact extraction requires a configured LLM. Raw records, explicit facts, existing memory reads, and database restoration work without one. Hashing embeddings remain available for lexical smoke checks; they do not establish semantic retrieval quality.

[中文](README.zh-CN.md) · [Documentation](docs/README.md) · [Quickstart](docs/quickstart.md)

## Try it

Start with the [deployment guide](deploy/README.md) to initialize Postgres and create an account and space. Use the [quickstart](docs/quickstart.md) to ingest a conversation and query the result.

The HTTP service, MCP tools, SDK clients, CLI and direct Python integrations use explicitly selected Postgres spaces. Install `mnemotrace-memory[serve]` for the service or `mnemotrace-memory[postgres,llm]` for the Python quickstart. The in-memory `MemoryEngine` class remains available for offline algorithm experiments.

## Choose an entry point


| Goal                           | Start here                                                                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Use and manage memory          | [Usage guide](docs/guides/usage.md)                                                                                                                  |
| Connect an agent or MCP client | [Agent integration](docs/guides/agents.md)                                                                                                           |
| Call an API                    | [Python](docs/reference/python-api.md), [HTTP](docs/reference/http-api.md), [MCP](docs/reference/mcp.md), [TypeScript](clients/typescript/README.md) |
| Configure models and storage   | [Configuration](docs/reference/configuration.md)                                                                                                     |
| Understand the implementation  | [Architecture](docs/architecture/README.md)                                                                                                          |
| Develop or measure changes     | [Contributing](CONTRIBUTING.md), [Evaluation](eval/README.md)                                                                                        |

Model-backed extraction, embeddings, and answering require the corresponding optional dependencies and configuration. The console requires a frontend build; installing the Python wheel alone does not include that build.

## License and security

See [LICENSE](LICENSE) and [commercial licensing terms](COMMERCIAL-LICENSE.md). Report vulnerabilities using [SECURITY.md](SECURITY.md). Changes are recorded in [CHANGELOG.md](CHANGELOG.md).
