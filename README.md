# MnemoTrace · 忆痕

[English](README.md) | [中文](README.zh-CN.md)

An open-source long-term memory engine for LLM agents. MnemoTrace keeps original conversations, extracts bi-temporal facts, preserves superseded history, and retrieves facts together with their source context.

**0.1.0 Beta · single-node self-hosting.** The offline loop needs no API keys or services. Production HTTP requires configured credentials. See [deployment](deploy/README.md), [security](SECURITY.md), and the [documentation](docs/README.md).

## Quickstart

Run from this checkout with Python 3.10 or newer:

```bash
python examples/quickstart.py
```

To use the package locally:

```bash
python -m pip install -e .
mnemotrace-quickstart
```

The distribution name is `mnemotrace-memory`; these commands install this checkout and do not imply an available PyPI/npm release. The core uses only the standard library, with deterministic hashing, rule extraction and memory stores.

```python
from mnemotrace import Memory

memory = Memory()
memory.add("My name is Alex and I work at Acme.", user_id="user123")
memory.consolidate()
print(memory.search("Where does Alex work?", user_id="user123").answer())
```

Use the exact same `user_id` to share memory across sessions. Different IDs remain separate; name/nickname recognition stays within one user. See the [quickstart](docs/quickstart.md), [Python API](docs/reference/python-api.md), and [backend status](docs/architecture/README.md#backend-status).

## Results

LongMemEval_S, 500 questions, official category-specific judge prompts. The recorded run uses doubao-seed-2.0-pro as answerer and the DeepSeek official judge; every compared system uses the same answerer, judge and questions.

| System | Accuracy | Average context tokens |
| --- | ---: | ---: |
| MnemoTrace (`engram_lean`) | **84.4%** | **7.9k** |
| Full-context baseline | 78.8% | 79.2k |

That run gives **+5.6 points** using about **1/10** of the baseline context tokens. These are historical measurements, not a live property of subsequent code. The earlier 83.6% / 73.2% run used another judge setup and remains separately documented.

Read [RESULTS.md](RESULTS.md) for exact settings, total-token and latency reporting limits, category results, provider changes, and reproduction commands. The [raw run](results/benchmarks/run500_v3_main_deepseekjudge.jsonl) is committed; use `python eval/report.py <file>` to recalculate it. [Evaluation tools](eval/README.md) include full-context comparison and completeness validation.

## Connect an agent or application

- [Agent guide](docs/guides/agents.md): local MCP, HTTP proxy, installation, doctor and session lifecycle.
- [HTTP API](docs/reference/http-api.md): protected service, console, pagination and OpenAI-compatible memory extension.
- [MCP tools](docs/reference/mcp.md) and [TypeScript SDK](clients/typescript/README.md).
- [Deployment](deploy/README.md): Docker Compose, systemd, backup, key rotation and upgrade boundaries.

Install optional capabilities from this checkout with `python -m pip install -e '.[serve]'`. Open `/ui/` after starting the configured server. Rules and the available optional backends are documented separately from planned integrations.

## Design and contributing

[Architecture](docs/architecture/README.md) explains writing, consolidation, retrieval and service boundaries. [Design decisions](docs/decisions.md) link to supporting and negative evidence. [Known issues](docs/issues.md) tracks unresolved behavior.

Start with [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Ordinary changes update the relevant source and tests; major changes use the scoped specification workflow.

[Paper](https://arxiv.org/abs/2606.09900) · [Paper sources](paper/README.md) · [Demo](https://notzhu.github.io/MnemoTrace/)

## License

AGPL-3.0; see [LICENSE](LICENSE). Proprietary or SaaS use that does not comply with AGPL requires a [commercial license](COMMERCIAL-LICENSE.md).
