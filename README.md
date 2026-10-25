# MnemoTrace · 忆痕

Long-term memory for LLM agents, with a Python library, HTTP service, MCP tools, TypeScript SDK, and a management console.

MnemoTrace stores conversation episodes, extracts facts with time and provenance, and retrieves relevant context across sessions. Memory updates can retain superseded facts for historical queries. The default Python path uses a hashing embedder and rule-based extraction: no API key, model download, or external service is required. These deterministic defaults demonstrate the pipeline; they do not establish semantic retrieval quality.

[中文](README.zh-CN.md) · [Documentation](docs/README.md) · [Quickstart](docs/quickstart.md)

## Try it

From a source checkout, with Python 3.10 or later:

```sh
python -m pip install .
mnemotrace-quickstart
```

For a minimal integration:

```python
from mnemotrace import Memory

memory = Memory()
memory.add("I live in Paris.", user_id="user123", session_id="demo")
memory.consolidate()
print(memory.search("Where do I live?", user_id="user123").answer())
# Paris
```

This instance lives in memory. See the [quickstart](docs/quickstart.md) to save and reopen it, or [deployment](deploy/README.md) to run a persistent service and console.

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
