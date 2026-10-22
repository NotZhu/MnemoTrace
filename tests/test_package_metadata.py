from __future__ import annotations

import importlib
import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 没有 tomllib
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_links_to_real_repository():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ts_package_text = (ROOT / "clients/typescript/package.json").read_text(encoding="utf-8")
    ts_package = json.loads(ts_package_text)
    ts_readme = (ROOT / "clients/typescript/README.md").read_text(encoding="utf-8")
    ts_index = (ROOT / "clients/typescript/src/index.ts").read_text(encoding="utf-8")
    quickstart_example = (ROOT / "examples/quickstart.py").read_text(encoding="utf-8")
    zero_setup = (ROOT / "scripts/check_zero_setup.py").read_text(encoding="utf-8")

    assert "your-org" not in pyproject
    assert "your-org" not in ts_package_text
    assert "your-org" not in ts_readme
    assert "your-org" not in ts_index
    assert 'Homepage = "https://github.com/NotZhu/MnemoTrace"' in pyproject
    assert 'Repository = "https://github.com/NotZhu/MnemoTrace"' in pyproject
    assert 'Documentation = "https://github.com/NotZhu/MnemoTrace#readme"' in pyproject
    assert 'Issues = "https://github.com/NotZhu/MnemoTrace/issues"' in pyproject
    assert 'license-files = ["LICENSE", "COMMERCIAL-LICENSE.md"]' in pyproject
    assert 'mnemotrace-quickstart = "mnemotrace.quickstart:main"' in pyproject
    assert "from mnemotrace.quickstart import main" in quickstart_example
    assert '"-m", "mnemotrace.quickstart"' in zero_setup
    for module in (
        "mnemotrace.agent_doctor",
        "mnemotrace.agent_setup",
        "mnemotrace.connectors",
        "mnemotrace.mcp",
    ):
        assert f'"{module}"' in zero_setup
    assert '"mnemotrace-agent-bootstrap"' in zero_setup
    assert "bootstrap_main(['--help'])" in zero_setup
    assert ts_package["repository"]["url"] == "https://github.com/NotZhu/MnemoTrace"
    assert ts_package["repository"]["directory"] == "clients/typescript"
    assert ts_package["homepage"] == "https://github.com/NotZhu/MnemoTrace/tree/main/clients/typescript"
    assert ts_package["bugs"]["url"] == "https://github.com/NotZhu/MnemoTrace/issues"
    assert ts_package["scripts"]["prepublishOnly"] == "npm run typecheck && npm run build && npm test"
    assert "](../../" not in ts_readme
    assert "https://github.com/NotZhu/MnemoTrace/blob/main/LICENSE" in ts_readme
    assert "https://github.com/NotZhu/MnemoTrace/blob/main/COMMERCIAL-LICENSE.md" in ts_readme


def test_python_console_scripts_resolve_to_callables():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert scripts
    for name, target in scripts.items():
        module_name, _, attr = target.partition(":")
        assert module_name and attr, f"{name} target must be module:callable"
        module = importlib.import_module(module_name)
        func = getattr(module, attr, None)
        assert callable(func), f"{name} target {target} is not callable"


def test_zero_setup_contract_mentions_installed_quickstart():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "mnemotrace-quickstart" in agents
    assert "python examples/quickstart.py" in agents
    assert "pytest" in agents

    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    # Claude imports must be outside Markdown code spans/fences.
    assert claude.splitlines().count("@AGENTS.md") == 1
    assert "`" not in claude
    assert not any(line.startswith("@") for line in agents.splitlines())
    assert "<!-- SPECKIT START -->" not in claude

    for rel, target in (
        ("CONTRIBUTING.md", "AGENTS.md#principle-ii"),
        (".specify/memory/constitution.md", "../../AGENTS.md#principle-ii"),
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert f"]({target})" in text
        assert "mnemotrace-quickstart" not in text, f"{rel} must not duplicate the contract"

    config = (ROOT / ".specify/extensions/agent-context/agent-context-config.yml").read_text(
        encoding="utf-8"
    )
    assert "context_file: AGENTS.md" in config
    assert agents.count("<!-- SPECKIT START -->") == 1
    assert agents.count("<!-- SPECKIT END -->") == 1


def test_console_seed_default_names_jsonl_stores_not_pickles():
    seed_console = (ROOT / "eval/seed_console.py").read_text(encoding="utf-8")

    assert 'default="/tmp/console_stores"' in seed_console
    assert "JSONL store directories" in seed_console
    assert "console_pkls" not in seed_console
    assert "sys.path.insert(0, str(ROOT))" in seed_console
