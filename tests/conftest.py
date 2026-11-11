"""Explicit execution switch for the paused history-import integration."""
import pytest

HISTORY_MODULES = {
    "test_connectors.py", "test_agent_sessions.py", "test_import_dedupe.py",
    "test_watch_install.py", "test_session_hook.py", "test_import_cli_product.py",
}


def pytest_addoption(parser):
    parser.addoption("--run-history-import", action="store_true", help="Run paused history-import tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "history_import: paused history-import integration")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.name in HISTORY_MODULES:
            item.add_marker(pytest.mark.history_import)
        if item.get_closest_marker("history_import") and not config.getoption("--run-history-import"):
            item.add_marker(pytest.mark.skip(reason="Paused history import; use --run-history-import"))
